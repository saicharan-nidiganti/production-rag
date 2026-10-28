"""Optional static API-key authentication."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


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
    if supplied is None or not hmac.compare_digest(
        supplied.encode(), expected.get_secret_value().encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
