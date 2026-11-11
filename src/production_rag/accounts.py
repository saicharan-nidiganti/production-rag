"""User accounts: passwords, browser sessions and personal API tokens (ADR-0012).

- **Passwords** are hashed with Argon2id (memory-hard, NFR-2) with the library's
  parameters stored in each hash, and re-hashed at sign-in when those parameters change.
  A sign-in for an address that doesn't exist does the same work as one that does, so
  timing doesn't reveal which addresses have accounts.
- **Sessions** are rows in the database. The browser holds a random token; the row's id is
  the token's SHA-256, so the database never holds anything that can be replayed. Each
  session carries a CSRF token, which the API requires on requests that change something.
- **API tokens** are for scripts: shown once, stored as a SHA-256, sent as a bearer token.

Every secret here is compared in constant time and none is ever logged.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from production_rag.config import Settings
from production_rag.db.models import ApiToken, AuthSession, Document, Role, User, utcnow

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "rag_"  # noqa: S105 - a public prefix that marks a string as one of our tokens
MAX_PASSWORD_LENGTH = 128  # bounds the work an attacker can ask the hash to do
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LAST_USED_RESOLUTION = timedelta(minutes=1)  # don't write the database on every request


class AccountError(ValueError):
    """A request the caller can fix; the message is safe to show them."""


class EmailTakenError(AccountError):
    pass


@dataclass(frozen=True)
class Principal:
    """Who a request acts as, and how they proved it."""

    user_id: str
    email: str
    role: Role
    via: Literal["session", "token"]
    csrf_token: str | None = None  # sessions only
    session_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if len(email) > 320 or not _EMAIL.match(email):
        raise AccountError("enter a valid email address")
    return email


class AccountService:
    def __init__(self, sessions: sessionmaker[Session], settings: Settings) -> None:
        self._sessions = sessions
        self._settings = settings
        self._hasher = PasswordHasher()
        # Verified for unknown addresses, so that path costs what a real check costs.
        self._decoy = self._hasher.hash(secrets.token_urlsafe(16))

    # --- Passwords ---

    def check_password_policy(self, password: str) -> None:
        minimum = self._settings.password_min_length
        if len(password) < minimum:
            raise AccountError(f"the password must be at least {minimum} characters")
        if len(password) > MAX_PASSWORD_LENGTH:
            raise AccountError(f"the password must be at most {MAX_PASSWORD_LENGTH} characters")

    def _verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password[:MAX_PASSWORD_LENGTH])
        except (VerificationError, InvalidHashError):
            return False

    # --- Users ---

    def create_user(self, email: str, password: str, role: Role = "user") -> User:
        email = normalize_email(email)
        self.check_password_policy(password)
        user = User(email=email, password_hash=self._hasher.hash(password), role=role)
        with self._sessions() as session:
            session.add(user)
            try:
                session.commit()
            except IntegrityError:
                raise EmailTakenError("an account with that email address already exists") from None
            session.refresh(user)
        logger.info("account.created", extra={"user_id": user.id, "role": role})
        return user

    def get_user(self, user_id: str) -> User | None:
        with self._sessions() as session:
            return session.get(User, user_id)

    def find_user(self, email: str) -> User | None:
        with self._sessions() as session:
            return session.scalar(select(User).where(User.email == email.strip().lower()))

    def list_users(self) -> list[User]:
        with self._sessions() as session:
            return list(session.scalars(select(User).order_by(User.created_at)))

    def count_users(self) -> int:
        return len(self.list_users())

    def set_password(self, user_id: str, password: str, *, keep_session: str | None = None) -> None:
        """Change the password and sign the user out everywhere else."""
        self.check_password_policy(password)
        with self._sessions() as session:
            session.execute(
                update(User)
                .where(User.id == user_id)
                .values(password_hash=self._hasher.hash(password), updated_at=utcnow())
            )
            session.execute(
                delete(AuthSession).where(
                    AuthSession.user_id == user_id, AuthSession.id != (keep_session or "")
                )
            )
            session.commit()
        logger.info("account.password_changed", extra={"user_id": user_id})

    def set_active(self, user_id: str, active: bool) -> bool:
        """Enable or disable an account; disabling signs it out. False if it doesn't exist."""
        with self._sessions() as session:
            user = session.get(User, user_id)
            if user is None:
                return False
            user.is_active = active
            user.updated_at = utcnow()
            if not active:
                session.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
            session.commit()
        logger.info("account.enabled" if active else "account.disabled", extra={"user_id": user_id})
        return True

    def set_role(self, user_id: str, role: Role) -> bool:
        with self._sessions() as session:
            user = session.get(User, user_id)
            if user is None:
                return False
            user.role = role
            user.updated_at = utcnow()
            session.commit()
        logger.info("account.role_changed", extra={"user_id": user_id, "role": role})
        return True

    def delete_user(self, user_id: str) -> bool:
        """Delete an account with its sessions, tokens and every document it owns."""
        with self._sessions() as session:
            user = session.get(User, user_id)
            if user is None:
                return False
            # Chunks, uploads and full-text entries go with the documents (cascade, triggers).
            session.execute(delete(Document).where(Document.owner_id == user_id))
            session.delete(user)
            session.commit()
        logger.info("account.deleted", extra={"user_id": user_id})
        return True

    def active_admins(self) -> int:
        return sum(1 for user in self.list_users() if user.role == "admin" and user.is_active)

    # --- Sign-in and sessions ---

    def authenticate(self, email: str, password: str) -> User | None:
        """The user, if the address and password match an active account."""
        user = self.find_user(email)
        if user is None:
            self._verify(self._decoy, password)
            return None
        if not self._verify(user.password_hash, password) or not user.is_active:
            return None
        if self._hasher.check_needs_rehash(user.password_hash):
            with self._sessions() as session:
                session.execute(
                    update(User)
                    .where(User.id == user.id)
                    .values(password_hash=self._hasher.hash(password))
                )
                session.commit()
        return user

    def start_session(self, user: User) -> tuple[str, Principal, datetime]:
        """A new session: the token for the cookie, who it is, and when it ends."""
        token = secrets.token_urlsafe(32)
        expires = utcnow() + timedelta(hours=self._settings.session_ttl_hours)
        row = AuthSession(
            id=_digest(token),
            user_id=user.id,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=expires,
        )
        with self._sessions() as session:
            session.execute(delete(AuthSession).where(AuthSession.expires_at < utcnow()))
            session.add(row)
            session.commit()
        logger.info("account.signed_in", extra={"user_id": user.id})
        return token, self._principal(user, "session", row.csrf_token, row.id), expires

    def end_session(self, session_id: str) -> None:
        with self._sessions() as session:
            session.execute(delete(AuthSession).where(AuthSession.id == session_id))
            session.commit()

    def resolve_session(self, token: str) -> Principal | None:
        with self._sessions() as session:
            row = session.execute(
                select(AuthSession, User)
                .join(User, User.id == AuthSession.user_id)
                .where(AuthSession.id == _digest(token), AuthSession.expires_at > utcnow())
            ).first()
        if row is None or not row.User.is_active:
            return None
        return self._principal(row.User, "session", row.AuthSession.csrf_token, row.AuthSession.id)

    # --- API tokens ---

    def create_token(self, user_id: str, name: str) -> tuple[str, ApiToken]:
        """A new token and its record. The token itself is not stored and can't be shown again."""
        name = name.strip()
        if not 1 <= len(name) <= 100:
            raise AccountError("give the token a name of 1 to 100 characters")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        record = ApiToken(user_id=user_id, name=name, token_hash=_digest(token))
        with self._sessions() as session:
            session.add(record)
            session.commit()
            session.refresh(record)
        logger.info("account.token_created", extra={"user_id": user_id, "token_id": record.id})
        return token, record

    def list_tokens(self, user_id: str) -> list[ApiToken]:
        with self._sessions() as session:
            return list(
                session.scalars(
                    select(ApiToken)
                    .where(ApiToken.user_id == user_id)
                    .order_by(ApiToken.created_at)
                )
            )

    def revoke_token(self, user_id: str, token_id: str) -> bool:
        """Delete one of the user's own tokens; False if it isn't theirs or doesn't exist."""
        with self._sessions() as session:
            result = session.execute(
                delete(ApiToken).where(ApiToken.id == token_id, ApiToken.user_id == user_id)
            )
            session.commit()
        revoked = bool(getattr(result, "rowcount", 0))
        if revoked:
            logger.info("account.token_revoked", extra={"user_id": user_id, "token_id": token_id})
        return revoked

    def resolve_token(self, token: str) -> Principal | None:
        if not token.startswith(TOKEN_PREFIX):
            return None
        now = utcnow()
        with self._sessions() as session:
            row = session.execute(
                select(ApiToken, User)
                .join(User, User.id == ApiToken.user_id)
                .where(ApiToken.token_hash == _digest(token))
            ).first()
            if row is None or not row.User.is_active:
                return None
            used = row.ApiToken.last_used_at
            if used is None or now - _aware(used) > _LAST_USED_RESOLUTION:
                session.execute(
                    update(ApiToken).where(ApiToken.id == row.ApiToken.id).values(last_used_at=now)
                )
                session.commit()
        return self._principal(row.User, "token")

    @staticmethod
    def _principal(
        user: User,
        via: Literal["session", "token"],
        csrf_token: str | None = None,
        session_id: str | None = None,
    ) -> Principal:
        return Principal(user.id, user.email, cast(Role, user.role), via, csrf_token, session_id)


def csrf_matches(supplied: str | None, expected: str | None) -> bool:
    return bool(supplied and expected and hmac.compare_digest(supplied.encode(), expected.encode()))


def _aware(moment: datetime) -> datetime:
    """SQLite returns stored UTC times without their zone."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
