"""Optional static API-key authentication.

``guard.RequestGuard`` checks the key before a request's body is read; ``require_api_key``
checks it again on every ``/v1`` route and documents the schemes in the OpenAPI schema.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr

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
