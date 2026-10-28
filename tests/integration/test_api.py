from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from production_rag.api import create_app
from production_rag.config import Settings
from production_rag.container import Container, build_container
from production_rag.generation import GeneratedAnswer, GenerationError
from production_rag.retrieval import RetrievedChunk
from tests.conftest import CORPUS_DIR

RUNBOOK = (CORPUS_DIR / "deployment-runbook.md").read_bytes()


def _upload(client: TestClient, name: str = "deployment-runbook.md", data: bytes = RUNBOOK, **kw):  # type: ignore[no-untyped-def]
    return client.post("/v1/documents", files={"file": (name, data)}, **kw)


def test_health_and_ready(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ready"
    assert body["checks"]["schema_revision"] == body["checks"]["expected_revision"] == "0001"
    assert body["checks"]["documents"] == 0


def test_ready_is_503_before_migrations(settings: Settings) -> None:
    container = build_container(settings)
    try:
        with TestClient(create_app(container=container)) as client:
            response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        assert client.get("/health").status_code == 200
    finally:
        container.close()


def test_document_lifecycle(client: TestClient) -> None:
    created = _upload(client)
    assert created.status_code == 201
    doc = created.json()["document"]
    assert created.json()["created"] is True
    assert doc["title"] == "Deployment Runbook" and doc["num_chunks"] >= 4

    duplicate = _upload(client, name="copy.md")
    assert duplicate.status_code == 200 and duplicate.json()["created"] is False
    assert duplicate.json()["document"]["id"] == doc["id"]

    listing = client.get("/v1/documents").json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == doc["id"]
    assert client.get(f"/v1/documents/{doc['id']}").json()["content_hash"] == doc["content_hash"]

    assert client.delete(f"/v1/documents/{doc['id']}").status_code == 204
    missing = client.get(f"/v1/documents/{doc['id']}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert client.delete(f"/v1/documents/{doc['id']}").status_code == 404


@pytest.mark.parametrize(
    ("name", "data", "status", "code"),
    [
        ("malware.exe", b"MZ", 415, "unsupported_document_type"),
        ("empty.md", b"\n\n", 422, "extraction_failed"),
        ("latin1.txt", "café".encode("latin-1"), 422, "extraction_failed"),
    ],
)
def test_upload_errors(client: TestClient, name: str, data: bytes, status: int, code: str) -> None:
    response = _upload(client, name=name, data=data)
    assert response.status_code == status
    body = response.json()["error"]
    assert body["code"] == code
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_upload_size_limit(container: Container, client: TestClient) -> None:
    container.settings.max_upload_bytes = 100
    response = _upload(client)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "document_too_large"


def test_search_and_query(client: TestClient) -> None:
    _upload(client)
    search = client.post(
        "/v1/search", json={"query": "rollback command", "top_k": 2, "mode": "hybrid"}
    )
    assert search.status_code == 200
    hits = search.json()["hits"]
    assert len(hits) == 2 and "Rollback" in hits[0]["heading"]
    assert "retrieval" in search.json()["timings_ms"]

    query = client.post("/v1/query", json={"question": "How long does the canary run?"})
    assert query.status_code == 200
    body = query.json()
    assert body["status"] == "answered"
    assert "30 minutes" in body["answer"]
    assert body["citations"] and body["model"] == "extractive-v1"
    for segment in body["segments"]:
        assert all(0 <= i < len(body["citations"]) for i in segment["citations"])
    assert body["answer"] == "".join(s["text"] for s in body["segments"])


def test_query_on_empty_index_abstains(client: TestClient) -> None:
    body = client.post("/v1/query", json={"question": "anything?"}).json()
    assert body["status"] == "insufficient_evidence"
    assert body["citations"] == [] and body["retrieved"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"question": ""},
        {"question": "x" * 2001},
        {"question": "ok", "top_k": 0},
        {"question": "ok", "extra": 1},
    ],
)
def test_query_validation(client: TestClient, payload: dict[str, object]) -> None:
    assert client.post("/v1/query", json=payload).status_code == 422


def test_request_id_is_echoed_or_generated(client: TestClient) -> None:
    assert (
        client.get("/health", headers={"X-Request-ID": "abc-123"}).headers["X-Request-ID"]
        == "abc-123"
    )
    generated = client.get("/health", headers={"X-Request-ID": "bad id with spaces"}).headers[
        "X-Request-ID"
    ]
    assert generated != "bad id with spaces" and len(generated) == 32


API_KEY = "top-secret-key-0123456789"


@pytest.fixture
def secured_client(container: Container) -> Iterator[TestClient]:
    container.settings.api_key = SecretStr(API_KEY)
    with TestClient(create_app(container=container)) as client:
        yield client


def test_api_key_is_required_when_configured(secured_client: TestClient) -> None:
    def status(headers: dict[str, str]) -> int:
        return secured_client.get("/v1/documents", headers=headers).status_code

    denied = secured_client.get("/v1/documents")
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "unauthorized"
    assert denied.headers["WWW-Authenticate"] == "Bearer"
    assert status({"X-API-Key": "wrong"}) == 401
    assert status({"X-API-Key": ""}) == 401
    assert status({"Authorization": f"Bearer {API_KEY}"}) == 200
    assert status({"X-API-Key": API_KEY}) == 200
    assert secured_client.get("/health").status_code == 200  # probes stay open


class _FailingGenerator:
    def __init__(self, error: Exception) -> None:
        self.error = error

    @property
    def model_id(self) -> str:
        return "failing"

    def generate(self, question: str, chunks: Sequence[RetrievedChunk]) -> GeneratedAnswer:
        raise self.error


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (GenerationError("overloaded", retryable=True), 503, "generation_unavailable"),
        (GenerationError("bad key", retryable=False), 502, "generation_failed"),
        (RuntimeError("boom"), 500, "internal_error"),
    ],
)
def test_generation_failures_map_to_http_errors(
    migrated_settings: Settings, error: Exception, status: int, code: str
) -> None:
    container = build_container(migrated_settings, generator=_FailingGenerator(error))
    container.ingestion.ingest("deployment-runbook.md", RUNBOOK)
    try:
        with TestClient(create_app(container=container), raise_server_exceptions=False) as client:
            response = client.post("/v1/query", json={"question": "How long does the canary run?"})
        assert response.status_code == status
        body = response.json()["error"]
        assert body["code"] == code
        assert body["request_id"] == response.headers["X-Request-ID"]
        assert "boom" not in body["message"]  # internal details are not leaked
    finally:
        container.close()
