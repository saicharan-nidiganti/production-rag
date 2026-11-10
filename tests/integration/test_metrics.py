"""GET /metrics: what it reports, who may read it, and how it behaves when things fail.

Counters are process-wide, so these tests compare scrapes taken before and after an action
rather than expecting absolute values.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from pydantic import SecretStr

from production_rag import __version__, metrics
from production_rag.api import create_app, monitoring
from production_rag.config import Settings
from production_rag.container import Container, build_container
from production_rag.generation import GeneratedAnswer, GenerationError
from production_rag.retrieval import RetrievedChunk
from tests.conftest import CORPUS_DIR

RUNBOOK = (CORPUS_DIR / "deployment-runbook.md").read_bytes()
API_KEY = "m" * 24  # obviously made up


class Scrape:
    def __init__(self, text: str) -> None:
        self.text = text
        self.samples = [
            (sample.name, sample.labels, sample.value)
            for family in text_string_to_metric_families(text)
            for sample in family.samples
        ]

    def value(self, name: str, **labels: str) -> float:
        """The sum of the samples called ``name`` whose labels include ``labels``."""
        return sum(
            value
            for sample, sample_labels, value in self.samples
            if sample == name and labels.items() <= sample_labels.items()
        )

    def has(self, name: str, **labels: str) -> bool:
        return any(
            sample == name and labels.items() <= sample_labels.items()
            for sample, sample_labels, _ in self.samples
        )


def scrape(client: TestClient, headers: Mapping[str, str] | None = None) -> Scrape:
    response = client.get("/metrics", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == metrics.CONTENT_TYPE
    return Scrape(response.text)


def wait_until_final(client: TestClient, document_id: str) -> str:
    deadline = time.monotonic() + 15
    while (status := client.get(f"/v1/documents/{document_id}").json()["status"]) not in (
        "ready",
        "failed",
    ):
        assert time.monotonic() < deadline, f"still {status}"
        time.sleep(0.02)
    return status


def test_requests_questions_and_ingestion_are_counted(client: TestClient) -> None:
    before = scrape(client)
    upload = client.post("/v1/documents", files={"file": ("runbook.md", RUNBOOK)})
    document_id = upload.json()["document"]["id"]
    assert wait_until_final(client, document_id) == "ready"
    assert client.post("/v1/query", json={"question": "How long does the canary run?"}).is_success
    after = scrape(client)

    def grew(name: str, **labels: str) -> float:
        return after.value(name, **labels) - before.value(name, **labels)

    # One series per route template, never one per document id.
    route = "/v1/documents/{document_id}"
    assert grew("rag_http_requests_total", method="GET", route=route, status="200") >= 1
    assert not after.has("rag_http_requests_total", route=f"/v1/documents/{document_id}")
    assert grew("rag_http_request_duration_seconds_count", method="POST", route="/v1/query") == 1
    assert grew("rag_ingestion_documents_total", outcome="ready") == 1
    assert grew("rag_ingestion_duration_seconds_count") == 1
    assert (
        grew("rag_ingested_chunks_total")
        == client.get(f"/v1/documents/{document_id}").json()["num_chunks"]
    )
    assert grew("rag_queries_total", streamed="false") == 1
    assert grew("rag_query_stage_seconds_count", stage="total") == 1
    assert grew("rag_query_stage_seconds_count", stage="retrieval") == 1

    # State read from the database at scrape time.
    assert after.value("rag_database_up") == 1
    assert after.value("rag_schema_at_head") == 1
    assert after.value("rag_documents", status="ready") == 1
    assert after.value("rag_documents", status="queued") == 0
    assert after.value("rag_chunks") > 0
    assert after.value("rag_stale_chunks") == 0
    assert after.value("rag_ingestion_oldest_queued_seconds") == 0
    assert after.value("rag_ingestion_workers") >= 1
    assert after.value("rag_build_info", version=__version__) == 1


def test_streamed_questions_are_counted_with_time_to_first_token(client: TestClient) -> None:
    upload = client.post("/v1/documents", files={"file": ("runbook.md", RUNBOOK)})
    wait_until_final(client, upload.json()["document"]["id"])
    before = scrape(client)
    with client.stream(
        "POST", "/v1/query/stream", json={"question": "How long does the canary run?"}
    ) as response:
        assert response.status_code == 200
        response.read()
    after = scrape(client)
    assert (
        after.value("rag_queries_total", streamed="true")
        - before.value("rag_queries_total", streamed="true")
        == 1
    )


def test_failed_ingestion_is_counted(client: TestClient) -> None:
    before = scrape(client)
    upload = client.post("/v1/documents", files={"file": ("empty.md", b"# Title\n\n## None\n")})
    assert wait_until_final(client, upload.json()["document"]["id"]) == "failed"
    after = scrape(client)
    assert (
        after.value("rag_ingestion_documents_total", outcome="failed")
        - before.value("rag_ingestion_documents_total", outcome="failed")
        == 1
    )
    assert after.value("rag_documents", status="failed") == 1


def test_unknown_paths_share_one_series(client: TestClient) -> None:
    for path in ("/nothing/1", "/nothing/2", "/v1/nothing"):
        assert client.get(path).status_code == 404
    after = scrape(client)
    assert after.value("rag_http_requests_total", route="unmatched", status="404") >= 3
    assert not any("nothing" in labels.get("route", "") for _, labels, _ in after.samples)


def test_a_waiting_document_shows_in_the_queue_age(container: Container) -> None:
    container.ingestion.submit("runbook.md", RUNBOOK)  # no app, so no worker claims it
    with TestClient(create_app(container=container)) as client:
        client.app.state.ingest_worker.stop()  # type: ignore[attr-defined]
        time.sleep(0.05)
        state = scrape(client)
    if state.value("rag_documents", status="queued") == 1:  # unless a worker got there first
        assert state.value("rag_ingestion_oldest_queued_seconds") > 0


@pytest.fixture
def secured(container: Container) -> Iterator[TestClient]:
    container.settings.api_key = SecretStr(API_KEY)
    with TestClient(create_app(container=container)) as client:
        yield client


def test_metrics_need_the_api_key_and_count_refusals(secured: TestClient) -> None:
    headers = {"Authorization": f"Bearer {API_KEY}"}
    before = scrape(secured, headers)
    denied = secured.get("/metrics")
    assert denied.status_code == 401
    assert "rag_http_requests_total" not in denied.text
    after = scrape(secured, headers)
    assert (
        after.value("rag_security_refusals_total", reason="auth_failed")
        - before.value("rag_security_refusals_total", reason="auth_failed")
        == 1
    )


def test_rate_limited_requests_are_counted_by_operation(container: Container) -> None:
    container.settings.rate_limit_search_per_minute = 1
    with TestClient(create_app(container=container)) as client:
        before = scrape(client)
        codes = [client.post("/v1/search", json={"query": "canary"}).status_code for _ in range(2)]
        after = scrape(client)
    assert codes == [200, 429]
    assert (
        after.value("rag_rate_limited_total", operation="search")
        - before.value("rag_rate_limited_total", operation="search")
        == 1
    )


def test_metrics_can_be_turned_off(container: Container) -> None:
    container.settings.metrics_enabled = False
    with TestClient(create_app(container=container)) as client:
        response = client.get("/metrics")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_scrape_reports_a_database_outage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_: Any) -> str:
        raise ConnectionError("database gone")

    monkeypatch.setattr(monitoring, "current_revision", unavailable)
    state = scrape(client)
    assert state.value("rag_database_up") == 0
    assert not state.has("rag_documents")
    assert state.has("rag_http_requests_total")  # the process's own metrics still answer


def test_a_schema_behind_head_is_reported_without_reading_tables(settings: Settings) -> None:
    container = build_container(settings)  # never migrated
    try:
        with TestClient(create_app(container=container)) as client:
            state = scrape(client)
    finally:
        container.close()
    assert state.value("rag_database_up") == 1
    assert state.value("rag_schema_at_head") == 0
    assert not state.has("rag_chunks")


class _Failing:
    def __init__(self, error: Exception) -> None:
        self.error = error

    @property
    def model_id(self) -> str:
        return "failing"

    def generate(self, question: str, chunks: Sequence[RetrievedChunk]) -> GeneratedAnswer:
        raise self.error


@pytest.mark.parametrize("retryable", [True, False])
def test_generation_failures_are_counted(migrated_settings: Settings, retryable: bool) -> None:
    container = build_container(
        migrated_settings, generator=_Failing(GenerationError("down", retryable=retryable))
    )
    container.ingestion.ingest("runbook.md", RUNBOOK)
    label = "true" if retryable else "false"
    try:
        with TestClient(create_app(container=container)) as client:
            before = scrape(client)
            response = client.post("/v1/query", json={"question": "How long does the canary run?"})
            after = scrape(client)
    finally:
        container.close()
    assert response.status_code == (503 if retryable else 502)
    assert (
        after.value("rag_generation_failures_total", retryable=label)
        - before.value("rag_generation_failures_total", retryable=label)
        == 1
    )


def test_token_and_refusal_counters() -> None:
    before = Scrape(metrics.exposition().decode())
    metrics.observe_generation("claude-test", 1200, 300)
    metrics.count_refusal(None)
    after = Scrape(metrics.exposition().decode())

    def grew(name: str, **labels: str) -> float:
        return after.value(name, **labels) - before.value(name, **labels)

    assert grew("rag_generation_tokens_total", model="claude-test", kind="input") == 1200
    assert grew("rag_generation_tokens_total", model="claude-test", kind="output") == 300
    assert grew("rag_generation_refusals_total", category="none") == 1
