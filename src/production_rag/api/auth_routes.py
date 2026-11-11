"""Sign-in, sessions, personal API tokens and user management (ADR-0012).

Every route except ``GET /v1/auth/status`` answers 404 when accounts are off.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from production_rag import metrics
from production_rag.accounts import AccountError, AccountService, EmailTakenError, Principal
from production_rag.api.errors import NotFoundError
from production_rag.api.guard import client_address
from production_rag.api.ratelimit import RateLimiter, client_key, per_minute
from production_rag.api.security import (
    SESSION_COOKIE,
    require_admin,
    require_session,
    require_user,
)
from production_rag.config import Settings
from production_rag.db.models import ApiToken, Role, User

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/v1/auth", tags=["accounts"])
users_router = APIRouter(prefix="/v1/users", tags=["accounts"])


def _accounts(request: Request) -> AccountService:
    accounts: AccountService = request.app.state.container.accounts
    return accounts


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.container.settings
    return settings


AccountsDep = Annotated[AccountService, Depends(_accounts)]
SettingsDep = Annotated[Settings, Depends(_settings)]
UserDep = Annotated[Principal, Depends(require_user)]
SessionDep = Annotated[Principal, Depends(require_session)]
AdminDep = Annotated[Principal, Depends(require_admin)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserOut(BaseModel):
    id: str
    email: str
    role: Role
    is_active: bool
    created_at: datetime

    @classmethod
    def of(cls, user: User) -> UserOut:
        return cls.model_validate(user, from_attributes=True)


class Me(BaseModel):
    id: str
    email: str
    role: Role


class AuthStatus(BaseModel):
    accounts_enabled: bool
    user: Me | None = Field(description="Who is signed in; null when nobody is.")
    csrf_token: str | None = Field(
        description="Send as X-CSRF-Token on requests that change something, when signed in "
        "with a browser session."
    )


class LoginRequest(_Body):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)


class PasswordChange(_Body):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class TokenCreate(_Body):
    name: str = Field(min_length=1, max_length=100)


class TokenOut(BaseModel):
    id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None

    @classmethod
    def of(cls, token: ApiToken) -> TokenOut:
        return cls.model_validate(token, from_attributes=True)


class TokenCreated(TokenOut):
    token: str = Field(description="Shown once. Send it as `Authorization: Bearer <token>`.")


class UserCreate(_Body):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)
    role: Literal["admin", "user"] = "user"


class UserUpdate(_Body):
    is_active: bool | None = None
    role: Literal["admin", "user"] | None = None
    password: str | None = Field(default=None, max_length=1024)


def _me(principal: Principal) -> Me:
    return Me(id=principal.user_id, email=principal.email, role=principal.role)


def _invalid(error: AccountError) -> HTTPException:
    code = status.HTTP_409_CONFLICT if isinstance(error, EmailTakenError) else 422
    return HTTPException(code, str(error))


# --- Sign-in ---


@auth_router.get("/status", response_model=AuthStatus)
def auth_status(request: Request, settings: SettingsDep) -> AuthStatus:
    """Whether accounts are on, and who is signed in. Never 401: the page asks this first."""
    principal: Principal | None = getattr(request.state, "principal", None)
    return AuthStatus(
        accounts_enabled=settings.accounts_enabled,
        user=_me(principal) if principal else None,
        csrf_token=principal.csrf_token if principal else None,
    )


@auth_router.post("/login", response_model=AuthStatus)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    accounts: AccountsDep,
    settings: SettingsDep,
) -> AuthStatus:
    if not settings.accounts_enabled:
        raise NotFoundError("accounts are not enabled")
    user = accounts.authenticate(body.email, body.password)
    if user is None:
        # Counted like a wrong API key: after a few, the client is refused before a password
        # is even checked (the guard does that), which slows guessing.
        limiter: RateLimiter = request.app.state.rate_limiter
        client = client_key(client_address(request.scope))
        limiter.wait_s(client, "auth_failure", per_minute(settings, "auth_failure"), take=True)
        logger.warning("security.login_failed", extra={"client": client_address(request.scope)})
        metrics.count_security_refusal("login_failed")
        # The same answer for a wrong password, an unknown address and a disabled account.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong email address or password")
    token, principal, expires = accounts.start_session(user)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_hours * 3600,
        expires=expires,
        httponly=True,  # scripts can't read it
        secure=settings.session_cookie_secure,
        samesite="strict",  # never sent on requests that start on another site
        path="/",
    )
    return AuthStatus(accounts_enabled=True, user=_me(principal), csrf_token=principal.csrf_token)


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(principal: UserDep, accounts: AccountsDep, settings: SettingsDep) -> Response:
    if principal.session_id is not None:
        accounts.end_session(principal.session_id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=settings.session_cookie_secure, samesite="strict"
    )
    return response


@auth_router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(body: PasswordChange, principal: SessionDep, accounts: AccountsDep) -> Response:
    """Change your own password; every other session of yours is signed out."""
    if accounts.authenticate(principal.email, body.current_password) is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "the current password is wrong")
    try:
        accounts.set_password(
            principal.user_id, body.new_password, keep_session=principal.session_id
        )
    except AccountError as error:
        raise _invalid(error) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Personal API tokens ---


@auth_router.get("/tokens", response_model=list[TokenOut])
def list_tokens(principal: UserDep, accounts: AccountsDep) -> list[TokenOut]:
    return [TokenOut.of(token) for token in accounts.list_tokens(principal.user_id)]


@auth_router.post("/tokens", response_model=TokenCreated, status_code=status.HTTP_201_CREATED)
def create_token(body: TokenCreate, principal: SessionDep, accounts: AccountsDep) -> TokenCreated:
    """A token for scripts. Needs a browser session: a token can't mint more tokens."""
    try:
        token, record = accounts.create_token(principal.user_id, body.name)
    except AccountError as error:
        raise _invalid(error) from None
    return TokenCreated(token=token, **TokenOut.of(record).model_dump())


@auth_router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_token(token_id: str, principal: UserDep, accounts: AccountsDep) -> Response:
    if not accounts.revoke_token(principal.user_id, token_id):
        raise NotFoundError(f"token {token_id} not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Users (administrators) ---


@users_router.get("", response_model=list[UserOut])
def list_users(_: AdminDep, accounts: AccountsDep) -> list[UserOut]:
    return [UserOut.of(user) for user in accounts.list_users()]


@users_router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, _: AdminDep, accounts: AccountsDep) -> UserOut:
    try:
        return UserOut.of(accounts.create_user(body.email, body.password, body.role))
    except AccountError as error:
        raise _invalid(error) from None


@users_router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: str, body: UserUpdate, _: AdminDep, accounts: AccountsDep) -> UserOut:
    user = accounts.get_user(user_id)
    if user is None:
        raise NotFoundError(f"user {user_id} not found")
    loses_admin = (
        user.role == "admin" and user.is_active and (body.is_active is False or body.role == "user")
    )
    if loses_admin and accounts.active_admins() <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "this is the only working administrator")
    try:
        if body.password is not None:
            accounts.set_password(user_id, body.password)
    except AccountError as error:
        raise _invalid(error) from None
    if body.role is not None:
        accounts.set_role(user_id, body.role)
    if body.is_active is not None:
        accounts.set_active(user_id, body.is_active)
    updated = accounts.get_user(user_id)
    if updated is None:  # deleted by another request meanwhile
        raise NotFoundError(f"user {user_id} not found")
    return UserOut.of(updated)


@users_router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: str, admin: AdminDep, accounts: AccountsDep) -> Response:
    """Delete an account and every document it owns."""
    user = accounts.get_user(user_id)
    if user is None:
        raise NotFoundError(f"user {user_id} not found")
    if user.id == admin.user_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "you can't delete your own account")
    if user.role == "admin" and user.is_active and accounts.active_admins() <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "this is the only working administrator")
    accounts.delete_user(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
