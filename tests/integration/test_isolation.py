"""Accounts over HTTP, and the cross-user isolation suite (NFR-1, ADR-0012).

Two users, Ada (an administrator) and Bob, share one server. Nothing either does may show,
change or be influenced by the other's documents: not a listing, a lookup by id, a search in
any mode, an answer, a citation, or a count.
"""

from __future__ import annotations

import io
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from production_rag.api import create_app
from production_rag.api.security import SESSION_COOKIE
from production_rag.cli import main
from production_rag.config import ConfigurationError, Settings, get_settings
from production_rag.container import Container
from production_rag.db import maintenance
from tests.conftest import CORPUS_DIR

PASSWORD = "correct horse battery"
RUNBOOK = (CORPUS_DIR / "deployment-runbook.md").read_bytes()
INCIDENTS = (CORPUS_DIR / "incident-response.md").read_bytes()
ADA_ONLY = b"# Ada's notes\n\n## Vault\n\nThe vault combination is nine-four-seven-two-tango.\n"
BOB_ONLY = b"# Bob's notes\n\n## Garden\n\nThe garden gate code is six-one-eight-zebra.\n"


class Site:
    """One running app, with a browser for each user."""

    def __init__(self, container: Container, anonymous: TestClient) -> None:
        self.container = container
        self.app = anonymous.app
        self.anonymous = anonymous
        self.accounts = container.accounts

    def browser(self) -> TestClient:
        return TestClient(self.app)  # its own cookie jar; the app is already started

    def sign_in(self, email: str, password: str = PASSWORD) -> TestClient:
        client = self.browser()
        response = client.post("/v1/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return client


@pytest.fixture
def site(container: Container) -> Iterator[Site]:
    container.settings.accounts_enabled = True
    container.settings.session_cookie_secure = False  # the test client speaks plain HTTP
    container.accounts.create_user("ada@example.com", PASSWORD, "admin")
    container.accounts.create_user("bob@example.com", PASSWORD)
    with TestClient(create_app(container=container)) as anonymous:
        yield Site(container, anonymous)


@pytest.fixture
def ada(site: Site) -> TestClient:
    return site.sign_in("ada@example.com")


@pytest.fixture
def bob(site: Site) -> TestClient:
    return site.sign_in("bob@example.com")


def upload(client: TestClient, name: str, data: bytes, **params: str) -> dict[str, Any]:
    response = client.post("/v1/documents", files={"file": (name, data)}, params=params)
    assert response.status_code in (200, 202), response.text
    document: dict[str, Any] = response.json()["document"]
    deadline = time.monotonic() + 15
    while document["status"] not in ("ready", "failed"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
        document = client.get(f"/v1/documents/{document['id']}").json()
    assert document["status"] == "ready", document
    return document


def ids(client: TestClient) -> set[str]:
    return {doc["id"] for doc in client.get("/v1/documents").json()["items"]}


# --- Signing in ---


def test_the_page_can_ask_who_is_signed_in_without_being_signed_in(
    site: Site, ada: TestClient
) -> None:
    nobody = site.anonymous.get("/v1/auth/status")
    assert nobody.status_code == 200
    assert nobody.json() == {"accounts_enabled": True, "user": None, "csrf_token": None}

    someone = ada.get("/v1/auth/status").json()
    assert someone["user"]["email"] == "ada@example.com" and someone["user"]["role"] == "admin"
    assert someone["csrf_token"] == ada.headers["X-CSRF-Token"]


def test_everything_else_needs_a_user(site: Site) -> None:
    for method, path in [("GET", "/v1/documents"), ("POST", "/v1/search"), ("GET", "/metrics"),
                         ("GET", "/v1/users"), ("GET", "/v1/auth/tokens")]:  # fmt: skip
        response = site.anonymous.request(method, path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "unauthorized"
    assert site.anonymous.get("/ready").status_code == 200  # probes stay open
    assert site.anonymous.get("/health").status_code == 200


def test_a_wrong_password_and_an_unknown_address_look_the_same(site: Site) -> None:
    wrong = site.anonymous.post(
        "/v1/auth/login", json={"email": "ada@example.com", "password": "not the password"}
    )
    unknown = site.anonymous.post(
        "/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert SESSION_COOKIE not in wrong.cookies


def test_repeated_failures_lock_the_client_out_even_with_the_right_password(site: Site) -> None:
    site.container.settings.rate_limit_auth_failures_per_minute = 3
    attempt = {"email": "ada@example.com", "password": "guess"}
    codes = [site.anonymous.post("/v1/auth/login", json=attempt).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    right = site.anonymous.post(
        "/v1/auth/login", json={"email": "ada@example.com", "password": PASSWORD}
    )
    assert right.status_code == 429 and "Retry-After" in right.headers


def test_the_session_cookie_is_hidden_from_scripts_and_other_sites(site: Site) -> None:
    site.container.settings.session_cookie_secure = True
    response = site.anonymous.post(
        "/v1/auth/login", json={"email": "ada@example.com", "password": PASSWORD}
    )
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
    assert "path=/" in cookie and "max-age=43200" in cookie
    token = response.cookies[SESSION_COOKIE]
    assert token not in response.text  # never in a body, where a script could read it


def test_changes_need_the_csrf_token_and_reads_dont(site: Site, ada: TestClient) -> None:
    forged = site.browser()
    forged.cookies.set(
        SESSION_COOKIE, ada.cookies[SESSION_COOKIE]
    )  # the cookie, as a browser sends it
    assert forged.get("/v1/documents").status_code == 200

    refused = forged.post("/v1/documents", files={"file": ("a.md", RUNBOOK)})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "csrf_failed"
    wrong = forged.post(
        "/v1/documents", files={"file": ("a.md", RUNBOOK)}, headers={"X-CSRF-Token": "guess"}
    )
    assert wrong.status_code == 403
    assert ids(ada) == set()  # nothing was stored
    assert forged.delete("/v1/documents/anything").status_code == 403


def test_signing_out_ends_the_session(site: Site, ada: TestClient) -> None:
    token = ada.cookies[SESSION_COOKIE]
    assert ada.post("/v1/auth/logout").status_code == 204
    replay = site.browser()
    replay.cookies.set(SESSION_COOKIE, token)
    assert replay.get("/v1/documents").status_code == 401


def test_changing_a_password_signs_out_everywhere_else(site: Site, ada: TestClient) -> None:
    other = site.sign_in("ada@example.com")
    wrong = ada.post(
        "/v1/auth/password", json={"current_password": "nope", "new_password": PASSWORD + " 2"}
    )
    assert wrong.status_code == 403
    weak = ada.post(
        "/v1/auth/password", json={"current_password": PASSWORD, "new_password": "short"}
    )
    assert weak.status_code == 422

    changed = ada.post(
        "/v1/auth/password", json={"current_password": PASSWORD, "new_password": PASSWORD + " 2"}
    )
    assert changed.status_code == 204
    assert ada.get("/v1/documents").status_code == 200
    assert other.get("/v1/documents").status_code == 401
    assert site.sign_in("ada@example.com", PASSWORD + " 2").get("/v1/documents").status_code == 200


# --- Personal API tokens ---


def test_a_token_acts_as_its_user_without_cookies_or_csrf(site: Site, bob: TestClient) -> None:
    created = bob.post("/v1/auth/tokens", json={"name": "nightly import"})
    assert created.status_code == 201
    token = created.json()["token"]
    script = site.browser()
    script.headers["Authorization"] = f"Bearer {token}"

    document = upload(script, "bob.md", BOB_ONLY)
    assert ids(bob) == {document["id"]}
    listed = bob.get("/v1/auth/tokens").json()
    assert [t["name"] for t in listed] == ["nightly import"] and "token" not in listed[0]

    # A token can't make more tokens or change the password: those need the password.
    assert script.post("/v1/auth/tokens", json={"name": "another"}).status_code == 403
    assert bob.delete(f"/v1/auth/tokens/{listed[0]['id']}").status_code == 204
    assert script.get("/v1/documents").status_code == 401


def test_one_user_cant_revoke_anothers_token(ada: TestClient, bob: TestClient) -> None:
    token_id = bob.post("/v1/auth/tokens", json={"name": "ci"}).json()["id"]
    assert ada.delete(f"/v1/auth/tokens/{token_id}").status_code == 404
    assert len(bob.get("/v1/auth/tokens").json()) == 1


# --- Administrators ---


def test_only_administrators_manage_users_and_read_metrics(
    ada: TestClient, bob: TestClient
) -> None:
    assert bob.get("/v1/users").status_code == 403
    assert (
        bob.post("/v1/users", json={"email": "x@example.com", "password": PASSWORD}).status_code
        == 403
    )
    assert bob.get("/metrics").status_code == 403
    assert ada.get("/metrics").status_code == 200
    assert [u["email"] for u in ada.get("/v1/users").json()] == [
        "ada@example.com",
        "bob@example.com",
    ]
    assert "password" not in str(ada.get("/v1/users").json()).lower()


def test_an_administrator_creates_disables_and_deletes_users(
    site: Site, ada: TestClient, bob: TestClient
) -> None:
    created = ada.post("/v1/users", json={"email": "Cy@Example.com", "password": PASSWORD})
    assert created.status_code == 201 and created.json()["email"] == "cy@example.com"
    assert (
        ada.post("/v1/users", json={"email": "cy@example.com", "password": PASSWORD}).status_code
        == 409
    )
    assert (
        ada.post("/v1/users", json={"email": "d@example.com", "password": "short"}).status_code
        == 422
    )
    assert (
        ada.post("/v1/users", json={"email": "not-an-address", "password": PASSWORD}).status_code
        == 422
    )

    bob_id = next(u["id"] for u in ada.get("/v1/users").json() if u["email"] == "bob@example.com")
    document = upload(bob, "bob.md", BOB_ONLY)
    assert ada.patch(f"/v1/users/{bob_id}", json={"is_active": False}).json()["is_active"] is False
    assert bob.get("/v1/documents").status_code == 401  # signed out at once

    assert ada.delete(f"/v1/users/{bob_id}").status_code == 204
    database = maintenance.database_file(site.container.settings.database_url)
    assert maintenance.verify(database).ok  # his documents and chunks went with him
    assert site.container.documents.get(document["id"]) is None


def test_the_last_administrator_cant_be_removed(ada: TestClient) -> None:
    me = ada.get("/v1/auth/status").json()["user"]["id"]
    assert ada.patch(f"/v1/users/{me}", json={"role": "user"}).status_code == 409
    assert ada.patch(f"/v1/users/{me}", json={"is_active": False}).status_code == 409
    assert ada.delete(f"/v1/users/{me}").status_code == 409
    assert ada.get("/v1/users").status_code == 200


# --- Isolation ---


@pytest.fixture
def libraries(ada: TestClient, bob: TestClient) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ada has the runbook and a private note; Bob has the incident policy and his own note."""
    upload(ada, "runbook.md", RUNBOOK)
    hers = upload(ada, "ada.md", ADA_ONLY)
    upload(bob, "incidents.md", INCIDENTS)
    his = upload(bob, "bob.md", BOB_ONLY)
    return hers, his


def test_each_user_lists_only_their_own_documents(
    ada: TestClient, bob: TestClient, libraries: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    hers, his = libraries
    assert len(ids(ada)) == 2 and len(ids(bob)) == 2
    assert ids(ada).isdisjoint(ids(bob))
    assert ada.get("/v1/documents").json()["total"] == 2  # the count doesn't include his
    assert hers["id"] in ids(ada) and his["id"] in ids(bob)


def test_every_route_that_takes_a_document_id_hides_anothers_document(
    site: Site, ada: TestClient, bob: TestClient, libraries: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    """Walks the OpenAPI schema, so a route added later is covered without editing this test."""
    hers, _ = libraries
    paths = ada.get("/openapi.json").json()["paths"]
    routes = [(m.upper(), p) for p, ops in paths.items() for m in ops if "{document_id}" in p]
    assert len(routes) >= 3
    for method, path in routes:
        mine = path.replace("{document_id}", hers["id"])
        response = bob.request(method, mine)
        missing = bob.request(method, path.replace("{document_id}", "no-such-document"))
        assert response.status_code == 404, f"{method} {path}"
        # Exactly what a document that doesn't exist gets: its existence isn't revealed.
        assert response.json()["error"]["code"] == missing.json()["error"]["code"]
    assert ada.get(f"/v1/documents/{hers['id']}").json()["status"] == "ready"  # untouched


def test_a_user_cant_replace_anothers_document(
    ada: TestClient, bob: TestClient, libraries: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    hers, _ = libraries
    before = ids(bob)
    response = bob.post(
        "/v1/documents", files={"file": ("x.md", b"# X\n\n## Y\n\nReplacement.\n")},
        params={"replaces": hers["id"]},
    )  # fmt: skip
    assert response.status_code == 404
    assert ids(bob) == before and hers["id"] in ids(ada)


@pytest.mark.parametrize("mode", ["lexical", "vector", "hybrid"])
def test_search_never_returns_anothers_passages(
    ada: TestClient, bob: TestClient, libraries: tuple[dict[str, Any], dict[str, Any]], mode: str
) -> None:
    def search(client: TestClient, query: str) -> list[dict[str, Any]]:
        response = client.post("/v1/search", json={"query": query, "mode": mode, "top_k": 50})
        assert response.status_code == 200
        hits: list[dict[str, Any]] = response.json()["hits"]
        return hits

    for query in ("vault combination tango", "garden gate code zebra", "canary rollback incident"):
        assert {hit["document_id"] for hit in search(ada, query)} <= ids(ada)
        assert {hit["document_id"] for hit in search(bob, query)} <= ids(bob)
    # Each finds their own secret, and the other's words find nothing of it.
    assert any("nine-four-seven" in hit["text"] for hit in search(ada, "vault combination"))
    assert not any("nine-four-seven" in hit["text"] for hit in search(bob, "vault combination"))
    assert any("six-one-eight" in hit["text"] for hit in search(bob, "garden gate code"))


def test_each_search_filters_by_owner_itself_not_afterwards(
    site: Site, ada: TestClient, bob: TestClient, libraries: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    """The retriever checks the owner again when it loads chunks, which would hide a search
    that forgot to. So each search is asked directly: with an owner, it must return that
    owner's chunk ids only, and as many as were asked for."""
    from sqlalchemy import text

    from production_rag.retrieval.lexical import lexical_search

    bob_id = bob.get("/v1/auth/status").json()["user"]["id"]
    retriever = site.container.rag.retriever
    with site.container.sessions() as session:
        owner_of = dict(
            session.execute(
                text(
                    "SELECT c.id, d.owner_id FROM chunks c JOIN documents d ON d.id = c.document_id"
                )
            ).all()
        )
        his_chunks = {chunk for chunk, owner in owner_of.items() if owner == bob_id}
        vector = retriever._embedder.embed_query("vault combination canary rollback incident")
        searches = {
            "lexical": lambda owner: lexical_search(
                session, "the canary incident vault", 100, owner
            ),
            "vector": lambda owner: retriever._index.search(session, vector, 100, owner),
        }
        for name, search in searches.items():
            everyone = {chunk for chunk, _ in search(None)}
            assert everyone - his_chunks, name  # without an owner, others' chunks do match
            mine = {chunk for chunk, _ in search(bob_id)}
            assert mine and mine <= his_chunks, name
            assert search("no-such-user") == [], name
        # The limit counts the owner's chunks: one is asked for, and it is his.
        assert {c for c, _ in retriever._index.search(session, vector, 1, bob_id)} <= his_chunks
        assert len(lexical_search(session, "the canary incident vault", 1, bob_id)) == 1


def test_answers_and_citations_come_only_from_the_askers_documents(
    ada: TestClient, bob: TestClient, libraries: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    question = {"question": "What is the vault combination?"}
    hers = ada.post("/v1/query", json=question).json()
    assert hers["status"] == "answered" and "nine-four-seven-two-tango" in hers["answer"]
    assert {c["document_id"] for c in hers["citations"]} <= ids(ada)

    his = bob.post("/v1/query", json=question).json()
    assert "nine-four-seven" not in str(his)  # not in the answer, a citation or a passage
    assert {hit["document_id"] for hit in his["retrieved"]} <= ids(bob)

    with bob.stream("POST", "/v1/query/stream", json=question) as stream:
        streamed = stream.read().decode()
    assert "nine-four-seven" not in streamed and "ada.md" not in streamed


def test_the_same_file_is_a_separate_document_for_each_user(
    site: Site, ada: TestClient, bob: TestClient
) -> None:
    first = ada.post("/v1/documents", files={"file": ("runbook.md", RUNBOOK)})
    second = bob.post("/v1/documents", files={"file": ("runbook.md", RUNBOOK)})
    # Bob isn't told the content already exists, which would reveal what Ada has.
    assert first.json()["created"] is True and second.json()["created"] is True
    hers, his = first.json()["document"]["id"], second.json()["document"]["id"]
    assert hers != his
    for client, document_id in ((ada, hers), (bob, his)):
        deadline = time.monotonic() + 15
        while client.get(f"/v1/documents/{document_id}").json()["status"] != "ready":
            assert time.monotonic() < deadline
            time.sleep(0.02)

    assert ada.delete(f"/v1/documents/{hers}").status_code == 204
    assert bob.get(f"/v1/documents/{his}").json()["status"] == "ready"
    assert bob.post("/v1/search", json={"query": "canary", "mode": "hybrid"}).json()["hits"]
    assert maintenance.verify(maintenance.database_file(site.container.settings.database_url)).ok


def test_a_small_library_isnt_crowded_out_by_a_large_one(ada: TestClient, bob: TestClient) -> None:
    """The limit applies to the asker's passages: filtering after the search would let Ada's
    many documents fill the candidates and leave Bob with nothing."""
    for path in sorted(CORPUS_DIR.glob("*.md")):
        upload(ada, path.name, path.read_bytes())
    upload(bob, "bob.md", BOB_ONLY)
    for mode in ("lexical", "vector", "hybrid"):
        hits = bob.post("/v1/search", json={"query": "the code", "mode": mode, "top_k": 1}).json()[
            "hits"
        ]
        assert [hit["source_name"] for hit in hits] == ["bob.md"], mode


def test_documents_from_before_accounts_belong_to_nobody(site: Site, ada: TestClient) -> None:
    site.container.ingestion.ingest("legacy.md", ADA_ONLY)  # the shared space, owner ''
    assert ids(ada) == set()
    assert ada.post("/v1/search", json={"query": "vault combination"}).json()["hits"] == []


# --- Configuration and the command line ---


def test_accounts_and_a_static_key_cant_be_combined(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_ACCOUNTS_ENABLED", "true")
    monkeypatch.setenv("RAG_API_KEY", "k" * 24)
    get_settings.cache_clear()
    with pytest.raises(ConfigurationError, match="can't both be set"):
        get_settings()


def test_with_accounts_off_the_account_routes_dont_exist(client: TestClient) -> None:
    assert client.get("/v1/auth/status").json() == {
        "accounts_enabled": False,
        "user": None,
        "csrf_token": None,
    }
    assert (
        client.post("/v1/auth/login", json={"email": "a@b.io", "password": "x"}).status_code == 404
    )
    assert client.get("/v1/users").status_code == 404
    assert client.get("/v1/auth/tokens").status_code == 404
    assert client.get("/v1/documents").status_code == 200  # the shared space, as before


def test_the_first_administrator_is_made_from_the_command_line(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RAG_DATABASE_URL", migrated_settings.database_url)
    monkeypatch.setenv("RAG_LOG_LEVEL", "WARNING")
    get_settings.cache_clear()

    def run(*args: str, stdin: str = "") -> tuple[int, str, str]:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        code = main(["users", *args])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    code, out, _ = run(
        "create", "ada@example.com", "--admin", "--password-stdin", stdin=PASSWORD + "\n"
    )
    assert code == 0 and '"role": "admin"' in out and PASSWORD not in out
    code, _, err = run("create", "ada@example.com", "--password-stdin", stdin=PASSWORD + "\n")
    assert code == 1 and "already exists" in err
    code, _, err = run("create", "b@example.com", "--password-stdin", stdin="short\n")
    assert code == 1 and "at least 12" in err

    assert run("list")[1].count("ada@example.com") == 1
    assert run("disable", "ada@example.com")[0] == 0
    assert '"active": false' in run("list")[1]
    assert (
        run("set-password", "ada@example.com", "--password-stdin", stdin=PASSWORD + " 2\n")[0] == 0
    )
    assert run("enable", "nobody@example.com")[0] == 1
    assert run("delete", "ada@example.com")[0] == 2  # needs --yes
    assert run("delete", "ada@example.com", "--yes")[0] == 0
    assert run("list")[1] == ""
