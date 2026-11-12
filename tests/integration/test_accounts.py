"""Accounts: passwords, sessions and personal tokens (accounts.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from production_rag.accounts import (
    TOKEN_PREFIX,
    AccountError,
    AccountService,
    EmailTakenError,
    csrf_matches,
    normalize_email,
)
from production_rag.container import Container
from tests.conftest import CORPUS_DIR, run_sql

PASSWORD = "correct horse battery"
RUNBOOK = (CORPUS_DIR / "deployment-runbook.md").read_bytes()


@pytest.fixture
def accounts(container: Container) -> AccountService:
    return container.accounts


raw = run_sql


# --- Users and passwords ---


def test_a_password_is_stored_only_as_an_argon2id_hash(
    container: Container, accounts: AccountService
) -> None:
    user = accounts.create_user("  Ada@Example.COM ", PASSWORD, "admin")
    assert user.email == "ada@example.com" and user.role == "admin" and user.is_active
    (stored,) = raw(container, "SELECT password_hash FROM users")[0]
    assert str(stored).startswith("$argon2id$") and PASSWORD not in str(stored)
    assert "m=65536" in str(stored)  # memory-hard: 64 MiB per guess (NFR-2)


@pytest.mark.parametrize(
    "email", ["", "no-at-sign", "a@b", "two words@example.com", "x" * 320 + "@a.io"]
)
def test_addresses_that_arent_email_are_refused(email: str) -> None:
    with pytest.raises(AccountError, match="valid email"):
        normalize_email(email)


def test_the_password_policy_bounds_length(accounts: AccountService) -> None:
    with pytest.raises(AccountError, match="at least 12"):
        accounts.create_user("a@example.com", "short")
    with pytest.raises(AccountError, match="at most 128"):
        accounts.create_user("a@example.com", "x" * 129)
    assert accounts.count_users() == 0


def test_an_address_has_one_account_whatever_its_case(accounts: AccountService) -> None:
    accounts.create_user("ada@example.com", PASSWORD)
    with pytest.raises(EmailTakenError):
        accounts.create_user("ADA@example.com", PASSWORD)


def test_sign_in_needs_the_right_password_and_an_active_account(accounts: AccountService) -> None:
    user = accounts.create_user("ada@example.com", PASSWORD)
    assert accounts.authenticate("ADA@example.com", PASSWORD) is not None
    assert accounts.authenticate("ada@example.com", PASSWORD + "x") is None
    assert accounts.authenticate("nobody@example.com", PASSWORD) is None

    assert accounts.set_active(user.id, False)
    assert accounts.authenticate("ada@example.com", PASSWORD) is None
    accounts.set_active(user.id, True)
    assert accounts.authenticate("ada@example.com", PASSWORD) is not None


def test_an_old_hash_is_upgraded_at_sign_in(container: Container, accounts: AccountService) -> None:
    from argon2 import PasswordHasher

    user = accounts.create_user("ada@example.com", PASSWORD)
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(PASSWORD)
    raw(container, "UPDATE users SET password_hash = ?", weak)
    assert accounts.authenticate("ada@example.com", PASSWORD) is not None
    (upgraded,) = raw(container, "SELECT password_hash FROM users WHERE id = ?", user.id)[0]
    assert "m=65536" in str(upgraded)


# --- Sessions ---


def test_a_session_is_stored_by_its_hash_and_resolves_to_the_user(
    container: Container, accounts: AccountService
) -> None:
    user = accounts.create_user("ada@example.com", PASSWORD)
    token, principal, expires = accounts.start_session(user)

    assert principal.user_id == user.id and principal.via == "session" and principal.csrf_token
    assert expires > datetime.now(UTC) + timedelta(hours=11)
    stored = [
        str(value)
        for row in raw(container, "SELECT id, csrf_token FROM auth_sessions")
        for value in row
    ]
    assert token not in stored  # a copy of the database can't be replayed as a session

    resolved = accounts.resolve_session(token)
    assert resolved == principal
    assert accounts.resolve_session(token + "x") is None
    assert accounts.resolve_session("") is None


def test_sessions_end_on_sign_out_expiry_disabling_and_a_password_change(
    container: Container, accounts: AccountService
) -> None:
    user = accounts.create_user("ada@example.com", PASSWORD)

    token, principal, _ = accounts.start_session(user)
    assert principal.session_id is not None
    accounts.end_session(principal.session_id)
    assert accounts.resolve_session(token) is None

    token, _, _ = accounts.start_session(user)
    raw(container, "UPDATE auth_sessions SET expires_at = '2020-01-01 00:00:00'")
    assert accounts.resolve_session(token) is None
    accounts.start_session(user)  # starting one clears expired rows
    assert raw(container, "SELECT COUNT(*) FROM auth_sessions") == [(1,)]

    token, _, _ = accounts.start_session(user)
    accounts.set_active(user.id, False)
    assert accounts.resolve_session(token) is None
    accounts.set_active(user.id, True)

    here, this, _ = accounts.start_session(user)
    elsewhere, _, _ = accounts.start_session(user)
    accounts.set_password(user.id, PASSWORD + " two", keep_session=this.session_id)
    assert accounts.resolve_session(here) is not None  # the browser that changed it stays in
    assert accounts.resolve_session(elsewhere) is None
    assert accounts.authenticate("ada@example.com", PASSWORD) is None
    assert accounts.authenticate("ada@example.com", PASSWORD + " two") is not None


def test_csrf_tokens_are_compared_exactly() -> None:
    assert csrf_matches("abc", "abc")
    assert not csrf_matches("abc", "abd")
    assert not csrf_matches(None, "abc") and not csrf_matches("abc", None)
    assert not csrf_matches("", "")


# --- API tokens ---


def test_a_token_is_shown_once_stored_as_a_hash_and_can_be_revoked(
    container: Container, accounts: AccountService
) -> None:
    ada = accounts.create_user("ada@example.com", PASSWORD)
    bob = accounts.create_user("bob@example.com", PASSWORD)
    token, record = accounts.create_token(ada.id, " laptop script ")

    assert token.startswith(TOKEN_PREFIX) and record.name == "laptop script"
    assert token not in str(raw(container, "SELECT * FROM api_tokens"))
    principal = accounts.resolve_token(token)
    assert principal is not None and principal.user_id == ada.id and principal.via == "token"
    assert principal.csrf_token is None
    assert raw(container, "SELECT COUNT(*) FROM api_tokens WHERE last_used_at IS NOT NULL") == [
        (1,)
    ]

    assert accounts.resolve_token(token + "x") is None
    assert accounts.resolve_token("not-one-of-ours") is None
    assert [t.id for t in accounts.list_tokens(ada.id)] == [record.id]
    assert accounts.list_tokens(bob.id) == []

    assert not accounts.revoke_token(bob.id, record.id)  # only its owner can revoke it
    assert accounts.resolve_token(token) is not None
    assert accounts.revoke_token(ada.id, record.id)
    assert accounts.resolve_token(token) is None


def test_a_disabled_users_token_stops_working(accounts: AccountService) -> None:
    user = accounts.create_user("ada@example.com", PASSWORD)
    token, _ = accounts.create_token(user.id, "ci")
    accounts.set_active(user.id, False)
    assert accounts.resolve_token(token) is None


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_a_token_needs_a_name(accounts: AccountService, name: str) -> None:
    user = accounts.create_user("ada@example.com", PASSWORD)
    with pytest.raises(AccountError, match="name"):
        accounts.create_token(user.id, name)


# --- Deleting an account ---


def test_deleting_a_user_removes_their_sessions_tokens_and_documents(
    container: Container, accounts: AccountService
) -> None:
    ada = accounts.create_user("ada@example.com", PASSWORD)
    bob = accounts.create_user("bob@example.com", PASSWORD)
    accounts.start_session(ada)
    accounts.create_token(ada.id, "ci")
    columns = (
        "id, source_name, title, content_type, content_hash, size_bytes, num_chunks, text,"
        " metadata, created_at, status, attempts, updated_at, owner_id"
    )
    values = (
        "'a.md', 'A', 'text/markdown', 'h', 1, 0, '', '{}', '2026-11-11', 'failed', 1, '2026-11-11'"
    )
    for document_id, owner in (("d-ada", ada.id), ("d-bob", bob.id)):
        # The same content hash for both: allowed, since each owner has their own copy.
        raw(
            container,
            f"INSERT INTO documents ({columns}) VALUES (?, {values}, ?)",
            document_id,
            owner,
        )

    assert accounts.delete_user(ada.id)
    assert not accounts.delete_user(ada.id)
    assert raw(container, "SELECT id FROM documents") == [("d-bob",)]
    assert raw(container, "SELECT COUNT(*) FROM auth_sessions") == [(0,)]
    assert raw(container, "SELECT COUNT(*) FROM api_tokens") == [(0,)]
    assert [u.email for u in accounts.list_users()] == ["bob@example.com"]


def test_roles_and_the_count_of_working_administrators(accounts: AccountService) -> None:
    ada = accounts.create_user("ada@example.com", PASSWORD, "admin")
    bob = accounts.create_user("bob@example.com", PASSWORD)
    assert accounts.active_admins() == 1
    assert accounts.set_role(bob.id, "admin") and accounts.active_admins() == 2
    accounts.set_active(ada.id, False)
    assert accounts.active_admins() == 1
    assert not accounts.set_role("missing", "admin") and not accounts.set_active("missing", True)
