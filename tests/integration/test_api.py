from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from production_rag.api import create_app
from production_rag.config import Settings
from production_rag.container import Container, build_container
from production_rag.db.migrate import head_revision
from production_rag.generation import GeneratedAnswer, GenerationError
from production_rag.retrieval import RetrievedChunk
from tests.conftest import CORPUS_DIR

RUNBOOK = (CORPUS_DIR / "deployment-runbook.md").read_bytes()


def _upload(client: TestClient, name: str = "deployment-runbook.md", data: bytes = RUNBOOK, **kw):  # type: ignore[no-untyped-def]
    return client.post("/v1/documents", files={"file": (name, data)}, **kw)


def _wait_for_final_status(
    client: TestClient, document_id: str, timeout_s: float = 15.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        doc: dict[str, Any] = client.get(f"/v1/documents/{document_id}").json()
        if doc["status"] in ("ready", "failed"):
            return doc
        assert time.monotonic() < deadline, f"still {doc['status']}"
        time.sleep(0.02)


def _upload_and_wait(
    client: TestClient, name: str = "deployment-runbook.md", data: bytes = RUNBOOK
) -> dict[str, Any]:
    response = _upload(client, name=name, data=data)
    assert response.status_code in (200, 202), response.text
    return _wait_for_final_status(client, response.json()["document"]["id"])


def test_health_and_ready(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ready"
    assert body["checks"]["schema_revision"] == body["checks"]["expected_revision"]
    assert body["checks"]["schema_revision"] == head_revision()
    assert body["checks"]["documents"] == 0
    assert body["checks"]["ingestion"] == {
        "queued": 0,
        "processing": 0,
        "ready": 0,
        "failed": 0,
        "workers": 1,
    }


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
    assert created.status_code == 202 and created.json()["created"] is True
    queued = created.json()["document"]
    assert created.headers["Location"] == f"/v1/documents/{queued['id']}"
    assert queued["status"] == "queued" and queued["error"] is None
    assert (queued["title"], queued["num_chunks"], queued["attempts"]) == (
        "deployment-runbook",
        0,
        0,
    )

    doc = _wait_for_final_status(client, queued["id"])
    assert doc["status"] == "ready" and doc["error"] is None and doc["attempts"] == 1
    assert doc["title"] == "Deployment Runbook" and doc["num_chunks"] >= 4
    assert doc["updated_at"] >= doc["created_at"]
    assert doc["created_at"] == queued["created_at"] and doc["created_at"].endswith("Z")

    duplicate = _upload(client, name="copy.md")
    assert duplicate.status_code == 200 and duplicate.json()["created"] is False
    assert duplicate.json()["document"]["id"] == doc["id"]
    assert duplicate.json()["document"]["status"] == "ready"

    listing = client.get("/v1/documents").json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == doc["id"]
    assert client.get("/v1/documents", params={"status": "ready"}).json()["total"] == 1
    assert client.get("/v1/documents", params={"status": "failed"}).json()["total"] == 0
    assert client.get("/v1/documents", params={"status": "bogus"}).status_code == 422
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
        ("fake.pdf", b"plain text", 415, "unsupported_document_type"),
    ],
)
def test_upload_errors(client: TestClient, name: str, data: bytes, status: int, code: str) -> None:
    response = _upload(client, name=name, data=data)
    assert response.status_code == status
    body = response.json()["error"]
    assert body["code"] == code
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_failed_processing_is_reported_and_can_be_retried(client: TestClient) -> None:
    """Problems found only by parsing are reported on the document, not by the upload."""
    upload = _upload(client, name="headings.md", data=b"# Title\n\n## Nothing here\n")
    assert upload.status_code == 202
    doc = _wait_for_final_status(client, upload.json()["document"]["id"])
    assert doc["status"] == "failed" and doc["num_chunks"] == 0
    assert doc["error"] == {
        "code": "extraction_failed",
        "message": "document contains no extractable text",
    }
    failed = client.get("/v1/documents", params={"status": "failed"}).json()
    assert [d["id"] for d in failed["items"]] == [doc["id"]]
    assert client.get("/ready").json()["checks"]["ingestion"]["failed"] == 1

    retried = client.post(f"/v1/documents/{doc['id']}/retry")
    assert retried.status_code == 202
    assert retried.json()["status"] == "queued" and retried.json()["error"] is None
    assert _wait_for_final_status(client, doc["id"])["status"] == "failed"  # same bytes

    reupload = _upload(client, name="headings.md", data=b"# Title\n\n## Nothing here\n")
    assert reupload.status_code == 202 and reupload.json()["created"] is False
    assert reupload.json()["document"]["status"] == "queued"


def test_retry_is_refused_unless_the_document_failed(client: TestClient) -> None:
    doc = _upload_and_wait(client)
    conflict = client.post(f"/v1/documents/{doc['id']}/retry")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "invalid_document_state"
    assert "this one is ready" in conflict.json()["error"]["message"]
    missing = client.post("/v1/documents/no-such-id/retry")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"


def test_queued_uploads_are_processed_after_a_restart(migrated_settings: Settings) -> None:
    """An upload accepted by one server process is processed by the next one."""
    no_workers = migrated_settings.model_copy(update={"ingest_workers": 0})
    first = build_container(no_workers)
    try:
        with TestClient(create_app(container=first)) as client:
            document_id = _upload(client).json()["document"]["id"]
            assert client.get("/ready").json()["checks"]["ingestion"]["workers"] == 0
            assert client.get(f"/v1/documents/{document_id}").json()["status"] == "queued"
    finally:
        first.close()

    second = build_container(migrated_settings)
    try:
        with TestClient(create_app(container=second)) as client:
            doc = _wait_for_final_status(client, document_id)
            assert doc["status"] == "ready" and doc["num_chunks"] >= 4
            hits = client.post("/v1/search", json={"query": "rollback command"}).json()["hits"]
            assert hits and hits[0]["document_id"] == document_id
    finally:
        second.close()


def test_upload_size_limit(container: Container, client: TestClient) -> None:
    container.settings.max_upload_bytes = 100
    response = _upload(client)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "document_too_large"


def test_search_and_query(client: TestClient) -> None:
    assert _upload_and_wait(client)["status"] == "ready"
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
    response = client.post("/v1/query", json=payload)
    assert response.status_code == 422
    # Same envelope as every other error, with the field-level problems attached.
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert error["details"] and all("loc" in d and "msg" in d for d in error["details"])


def test_unknown_routes_use_the_error_envelope(client: TestClient) -> None:
    response = client.get("/v1/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert "details" not in response.json()["error"]


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
