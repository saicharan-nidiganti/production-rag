"""Checks that run before the application reads a request body (issue #30).

FastAPI parses a request's body, including a multipart upload, before route dependencies
run, and Starlette spools an upload over 1 MB to a temporary file while it does. A size limit
or an API-key check made in a route or a dependency therefore comes after the server has
received, and written to disk, whatever the client sent. This middleware runs first:

1. Every path except the probes and the API docs needs the API key, when ``RAG_API_KEY`` is
   set. The route dependency (``security.require_api_key``) checks it again. A client that
   keeps failing gets 429 without its key being compared at all.
2. Requests over a per-client rate limit get 429 with ``Retry-After`` (``ratelimit.py``,
   which also explains why those limits are per process).
3. A declared ``Content-Length`` over the limit is refused with 413 before any of the body is
   read. A body without one (chunked) is counted as it arrives and refused at the limit.

Each refusal is logged as a ``security.*`` event with the client address, never with the
credentials or the body.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import anyio
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from production_rag import metrics
from production_rag.accounts import Principal, csrf_matches
from production_rag.api.errors import error_response
from production_rag.api.ratelimit import (
    Operation,
    RateLimiter,
    client_key,
    operation_for,
    per_minute,
    retry_after_header,
)
from production_rag.api.security import (
    CSRF_HEADER,
    SAFE_METHODS,
    key_matches,
    presented_credentials,
    supplied_key,
)
from production_rag.api.web import is_open
from production_rag.config import Settings

logger = logging.getLogger(__name__)

# Paths anyone may call: liveness and readiness probes, and the API documentation.
OPEN_PATHS = frozenset(
    {"/health", "/ready", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
)

# With accounts on: signing in can't need a signed-in user, and the status route answers
# either way, so the web interface can ask who, if anyone, is signed in.
LOGIN_PATH = "/v1/auth/login"
STATUS_PATH = "/v1/auth/status"

Authenticator = Callable[[Headers], Principal | None]

UPLOAD_ROUTE = ("POST", "/v1/documents")
# Multipart framing around the file: boundaries and part headers, well under this.
MULTIPART_ALLOWANCE = 64 * 1024


class RequestTooLargeError(HTTPException):
    """Raised while the body is read, so FastAPI's body parsing passes it on unchanged."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(status_code=413, detail=message)
        self.code = code


def client_address(scope: Scope) -> str | None:
    """The peer address, or the forwarded one when uvicorn trusts the proxy that sent it."""
    client = scope.get("client")
    return str(client[0]) if client else None


class RequestGuard:
    def __init__(
        self,
        app: ASGIApp,
        settings: Settings,
        limiter: RateLimiter | None = None,
        authenticator: Authenticator | None = None,
    ) -> None:
        self.authenticator = authenticator
        self.app = app
        # Read on every request, not copied here: tests change settings on a live app.
        self.settings = settings
        self.limiter = limiter or RateLimiter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        refusal = None
        if scope["path"] not in OPEN_PATHS and not is_open(scope["path"]):
            client = client_key(client_address(scope))
            if self.settings.accounts_enabled:
                refusal = await self._authenticate_user(scope, headers, client)
            else:
                refusal = self._authenticate(scope, headers, client)
            refusal = refusal or self._rate_limit(scope, client)
        refusal = refusal or self._check_declared_size(scope, headers)
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        await self.app(scope, self._counting(scope, receive), send)

    # --- Authentication ---

    def _authenticate(self, scope: Scope, headers: Headers, client: str) -> ASGIApp | None:
        expected = self.settings.api_key
        if expected is None:
            return None
        limit = per_minute(self.settings, "auth_failure")
        # A client out of attempts isn't told whether this key would have worked.
        if wait := self.limiter.wait_s(client, "auth_failure", limit, take=False):
            return self._too_many(scope, "auth_failure", wait)
        supplied = supplied_key(headers)
        if key_matches(supplied, expected):
            return None
        self.limiter.wait_s(client, "auth_failure", limit, take=True)
        logger.warning(
            "security.auth_failed",
            extra={
                "reason": "missing" if supplied is None else "invalid",
                **_where(scope),
            },
        )
        metrics.count_security_refusal("auth_failed")
        response = error_response(401, "unauthorized", "missing or invalid API key")
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    async def _authenticate_user(
        self, scope: Scope, headers: Headers, client: str
    ) -> ASGIApp | None:
        """Accounts: find the user from the session cookie or a personal token, and require
        the session's CSRF token on anything that changes state."""
        limit = per_minute(self.settings, "auth_failure")
        if wait := self.limiter.wait_s(client, "auth_failure", limit, take=False):
            return self._too_many(scope, "auth_failure", wait)
        path = scope["path"]
        if path == LOGIN_PATH:
            return None  # the route checks the password, and counts a failure against the client
        principal = None
        if self.authenticator is not None:
            # A database read: off the event loop.
            principal = await anyio.to_thread.run_sync(self.authenticator, headers)
        if principal is None:
            if path == STATUS_PATH:
                return None
            presented = any(presented_credentials(headers))
            if presented:  # a first visit with no credentials isn't a failed attempt
                self.limiter.wait_s(client, "auth_failure", limit, take=True)
            logger.warning(
                "security.auth_failed",
                extra={"reason": "invalid" if presented else "missing", **_where(scope)},
            )
            metrics.count_security_refusal("auth_failed")
            response = error_response(401, "unauthorized", "sign in, or send a valid API token")
            response.headers["WWW-Authenticate"] = "Bearer"
            return response
        if (
            principal.via == "session"
            and scope["method"] not in SAFE_METHODS
            and not csrf_matches(headers.get(CSRF_HEADER), principal.csrf_token)
        ):
            logger.warning("security.csrf_failed", extra=_where(scope))
            metrics.count_security_refusal("csrf_failed")
            return error_response(403, "csrf_failed", "missing or wrong X-CSRF-Token header")
        scope.setdefault("state", {})["principal"] = principal
        return None

    # --- Rate limits ---

    def _rate_limit(self, scope: Scope, client: str) -> ASGIApp | None:
        operation = operation_for(scope["method"], scope["path"])
        limit = per_minute(self.settings, operation)
        if wait := self.limiter.wait_s(client, operation, limit, take=True):
            return self._too_many(scope, operation, wait)
        return None

    def _too_many(self, scope: Scope, operation: Operation, wait_s: float) -> ASGIApp:
        retry_after = retry_after_header(wait_s)
        logger.warning(
            "security.rate_limited",
            extra={"operation": operation, "retry_after_s": int(retry_after), **_where(scope)},
        )
        metrics.count_security_refusal("rate_limited", operation)
        what = "failed API-key attempts" if operation == "auth_failure" else f"{operation} requests"
        response = error_response(
            429, "rate_limited", f"too many {what}; retry after {retry_after} s"
        )
        response.headers["Retry-After"] = retry_after
        return response

    # --- Body size ---

    def _limit(self, scope: Scope) -> tuple[int, str, str]:
        """(bytes allowed, error code, message) for this request's body."""
        if (scope["method"], scope["path"]) == UPLOAD_ROUTE:
            limit = self.settings.max_upload_bytes
            # The route checks the file itself against the exact limit.
            message = f"document exceeds the {limit}-byte limit (RAG_MAX_UPLOAD_BYTES)"
            return limit + MULTIPART_ALLOWANCE, "document_too_large", message
        limit = self.settings.max_request_bytes
        message = f"request body exceeds the {limit}-byte limit (RAG_MAX_REQUEST_BYTES)"
        return limit, "request_too_large", message

    def _check_declared_size(self, scope: Scope, headers: Headers) -> ASGIApp | None:
        try:
            declared = int(headers.get("content-length", ""))
        except ValueError:
            return None  # none declared: counted as it arrives
        limit, code, message = self._limit(scope)
        if declared <= limit:
            return None
        logger.warning(
            "security.request_too_large",
            extra={"limit_bytes": limit, "declared_bytes": declared, **_where(scope)},
        )
        metrics.count_security_refusal("request_too_large")
        return error_response(413, code, message)

    def _counting(self, scope: Scope, receive: Receive) -> Callable[[], Awaitable[Message]]:
        limit, code, message = self._limit(scope)
        received = 0

        async def receive_within_limit() -> Message:
            nonlocal received
            event = await receive()
            if event["type"] == "http.request":
                received += len(event.get("body", b""))
                if received > limit:
                    logger.warning(
                        "security.request_too_large",
                        extra={"limit_bytes": limit, "received_bytes": received, **_where(scope)},
                    )
                    metrics.count_security_refusal("request_too_large")
                    raise RequestTooLargeError(code, message)
            return event

        return receive_within_limit


def _where(scope: Scope) -> dict[str, str | None]:
    return {"client": client_address(scope), "method": scope["method"], "path": scope["path"]}
