"""Checks that run before the application reads a request body (issue #30).

FastAPI parses a request's body, including a multipart upload, before route dependencies
run, and Starlette spools an upload over 1 MB to a temporary file while it does. A size limit
or an API-key check made in a route or a dependency therefore comes after the server has
received, and written to disk, whatever the client sent. This middleware runs first:

1. Every path except the probes and the API docs needs the API key, when ``RAG_API_KEY`` is
   set. The route dependency (``security.require_api_key``) checks it again.
2. A declared ``Content-Length`` over the limit is refused with 413 before any of the body is
   read. A body without one (chunked) is counted as it arrives and refused at the limit.

Each refusal is logged as a ``security.*`` event with the client address, never with the
credentials or the body.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from production_rag.api.errors import error_response
from production_rag.api.security import key_matches, supplied_key
from production_rag.config import Settings

logger = logging.getLogger(__name__)

# Paths anyone may call: liveness and readiness probes, and the API documentation.
OPEN_PATHS = frozenset(
    {"/health", "/ready", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
)

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
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        # Read on every request, not copied here: tests change settings on a live app.
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        refusal = self._authenticate(scope, headers) or self._check_declared_size(scope, headers)
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        await self.app(scope, self._counting(scope, receive), send)

    # --- Authentication ---

    def _authenticate(self, scope: Scope, headers: Headers) -> ASGIApp | None:
        expected = self.settings.api_key
        if expected is None or scope["path"] in OPEN_PATHS:
            return None
        supplied = supplied_key(headers)
        if key_matches(supplied, expected):
            return None
        logger.warning(
            "security.auth_failed",
            extra={
                "reason": "missing" if supplied is None else "invalid",
                **_where(scope),
            },
        )
        response = error_response(401, "unauthorized", "missing or invalid API key")
        response.headers["WWW-Authenticate"] = "Bearer"
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
                    raise RequestTooLargeError(code, message)
            return event

        return receive_within_limit


def _where(scope: Scope) -> dict[str, str | None]:
    return {"client": client_address(scope), "method": scope["method"], "path": scope["path"]}
