"""Who may call the API: nobody in particular, the holder of a static key, or a user.

- With neither ``RAG_API_KEY`` nor ``RAG_ACCOUNTS_ENABLED``, the API is open.
- With ``RAG_API_KEY``, every ``/v1`` request needs that key, and all documents are shared.
- With ``RAG_ACCOUNTS_ENABLED``, every ``/v1`` request acts as a user, identified by a
  session cookie or a personal API token, and reads only that user's documents.

``guard.RequestGuard`` decides this before a request's body is read and leaves the user in
``request.state.principal``. The dependencies here check it again on each route and give
routes the owner whose documents they may touch.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr
from starlette.requests import cookie_parser

from production_rag.accounts import AccountService, Principal
from production_rag.db.models import SHARED_OWNER

SESSION_COOKIE = "rag_session"
CSRF_HEADER = "x-csrf-token"
# Methods that change nothing, so a cross-site request made with them can do no harm.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_bearer = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def supplied_key(headers: Mapping[str, str]) -> str | None:
    """The key from ``Authorization: Bearer <key>``, else from ``X-API-Key``; None if neither."""
    scheme, _, credentials = headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and credentials.strip():
        return credentials.strip()
    return headers.get("x-api-key")


def key_matches(supplied: str | None, expected: SecretStr) -> bool:
    """Constant-time comparison, so response timing doesn't reveal how much of a key matched."""
    return supplied is not None and hmac.compare_digest(
        supplied.encode(), expected.get_secret_value().encode()
    )


def require_api_key(
    request: Request,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    header_key: Annotated[str | None, Depends(_api_key_header)],
) -> None:
    """Accept ``Authorization: Bearer <key>`` or ``X-API-Key: <key>`` when RAG_API_KEY is set."""
    expected = request.app.state.container.settings.api_key
    if expected is None:
        return
    supplied = bearer.credentials if bearer else header_key
    if not key_matches(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


def presented_credentials(headers: Mapping[str, str]) -> tuple[str | None, str | None]:
    """(session token from the cookie, bearer token), either of which may be missing."""
    cookie = cookie_parser(headers.get("cookie", "")).get(SESSION_COOKIE) or None
    scheme, _, credentials = headers.get("authorization", "").partition(" ")
    bearer = credentials.strip() if scheme.lower() == "bearer" and credentials.strip() else None
    return cookie, bearer


def resolve_principal(headers: Mapping[str, str], accounts: AccountService) -> Principal | None:
    """The user a request acts as. A bearer token wins over a cookie: a script that sends one
    means it, while a browser sends its cookie with everything."""
    cookie, bearer = presented_credentials(headers)
    if bearer is not None:
        return accounts.resolve_token(bearer)
    if cookie is not None:
        return accounts.resolve_session(cookie)
    return None


def current_principal(request: Request) -> Principal | None:
    """The signed-in user, when accounts are on; None when they are off."""
    if not request.app.state.container.settings.accounts_enabled:
        return None
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:  # the guard refuses these first; this is the second check
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in to continue")
    return principal


PrincipalDep = Annotated[Principal | None, Depends(current_principal)]


def current_owner(principal: PrincipalDep) -> str:
    """Whose documents this request may read and change."""
    return principal.user_id if principal is not None else SHARED_OWNER


OwnerDep = Annotated[str, Depends(current_owner)]


def require_user(principal: PrincipalDep) -> Principal:
    if principal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "accounts are not enabled")
    return principal


def require_admin(principal: Annotated[Principal, Depends(require_user)]) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this needs an administrator")
    return principal


def require_session(principal: Annotated[Principal, Depends(require_user)]) -> Principal:
    """For actions a stolen API token must not be able to do, such as minting more tokens."""
    if principal.via != "session":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "sign in with your password to do this")
    return principal
