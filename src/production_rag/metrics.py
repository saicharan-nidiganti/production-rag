"""Prometheus metrics, served at ``GET /metrics`` (docs/observability.md).

Counters and histograms are updated at the same points that log the matching events, so the
metrics and the logs agree. The state of the index and the queue (documents by status,
chunks, live workers) is read from the database at scrape time instead.

Each process keeps its own values, like the rate limits: with ``uvicorn --workers N``, a
scrape reaches one of the N processes. Run one process per scrape target, or aggregate the
targets in Prometheus.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)
from prometheus_client.metrics_core import Metric

from production_rag import __version__

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

REGISTRY = CollectorRegistry()
ProcessCollector(registry=REGISTRY)  # CPU, memory, open files (Linux only)
PlatformCollector(registry=REGISTRY)
GCCollector(registry=REGISTRY)

_REQUEST_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
_INGEST_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)

BUILD_INFO = Gauge(
    "rag_build_info", "The running version; always 1.", ["version"], registry=REGISTRY
)
BUILD_INFO.labels(__version__).set(1)

HTTP_REQUESTS = Counter(
    "rag_http_requests",
    "HTTP requests, by method, route template and status code.",
    ["method", "route", "status"],
    registry=REGISTRY,
)
HTTP_DURATION = Histogram(
    "rag_http_request_duration_seconds",
    "Time until the response started: for a stream, until its headers were sent.",
    ["method", "route"],
    buckets=_REQUEST_BUCKETS,
    registry=REGISTRY,
)
QUERIES = Counter(
    "rag_queries",
    "Questions answered, by answer status and whether the answer was streamed.",
    ["status", "streamed"],
    registry=REGISTRY,
)
ABSTENTIONS = Counter(
    "rag_query_abstentions_before_generation",
    "Questions answered insufficient_evidence without calling the generator.",
    registry=REGISTRY,
)
QUERY_STAGE = Histogram(
    "rag_query_stage_seconds",
    "Time per stage of a question: retrieval, rerank, generation, first_token and total.",
    ["stage"],
    buckets=_REQUEST_BUCKETS,
    registry=REGISTRY,
)
INGESTIONS = Counter(
    "rag_ingestion_documents",
    "Documents processed by a worker, by outcome: ready, failed, released or lost.",
    ["outcome"],
    registry=REGISTRY,
)
INGESTION_DURATION = Histogram(
    "rag_ingestion_duration_seconds",
    "Time to process one document, from claim to ready or failed.",
    buckets=_INGEST_BUCKETS,
    registry=REGISTRY,
)
INGESTED_CHUNKS = Counter(
    "rag_ingested_chunks", "Chunks written by completed documents.", registry=REGISTRY
)
GENERATION_TOKENS = Counter(
    "rag_generation_tokens",
    "Model tokens reported by the provider, by model and kind (input or output).",
    ["model", "kind"],
    registry=REGISTRY,
)
GENERATION_FAILURES = Counter(
    "rag_generation_failures",
    "Generation requests that failed, by whether the failure is worth retrying.",
    ["retryable"],
    registry=REGISTRY,
)
GENERATION_REFUSALS = Counter(
    "rag_generation_refusals",
    "Answers the model declined, by refusal category (none when the API gave none).",
    ["category"],
    registry=REGISTRY,
)
SECURITY_REFUSALS = Counter(
    "rag_security_refusals",
    "Requests the request guard refused: auth_failed, rate_limited or request_too_large.",
    ["reason"],
    registry=REGISTRY,
)
RATE_LIMITED = Counter(
    "rag_rate_limited",
    "Requests refused for a rate limit, by operation.",
    ["operation"],
    registry=REGISTRY,
)
VECTOR_EXACT_FALLBACKS = Counter(
    "rag_vector_exact_fallbacks",
    "Approximate vector searches that found fewer results than asked for and were redone "
    "exactly: an owner with few chunks among many, or a library smaller than the request.",
    registry=REGISTRY,
)


def observe_http(method: str, route: str, status: int, seconds: float) -> None:
    HTTP_REQUESTS.labels(method, route, str(status)).inc()
    HTTP_DURATION.labels(method, route).observe(seconds)


def observe_query(
    *, status: str, streamed: bool, abstained: bool, timings_ms: Mapping[str, float]
) -> None:
    QUERIES.labels(status, "true" if streamed else "false").inc()
    if abstained:
        ABSTENTIONS.inc()
    for stage, milliseconds in timings_ms.items():
        QUERY_STAGE.labels(stage).observe(milliseconds / 1000)


def observe_ingestion(outcome: str, seconds: float, chunks: int = 0) -> None:
    INGESTIONS.labels(outcome).inc()
    if outcome in ("ready", "failed"):
        INGESTION_DURATION.observe(seconds)
    if chunks:
        INGESTED_CHUNKS.inc(chunks)


def observe_generation(model: str, input_tokens: int, output_tokens: int) -> None:
    GENERATION_TOKENS.labels(model, "input").inc(input_tokens)
    GENERATION_TOKENS.labels(model, "output").inc(output_tokens)


def count_generation_failure(*, retryable: bool) -> None:
    GENERATION_FAILURES.labels("true" if retryable else "false").inc()


def count_refusal(category: str | None) -> None:
    GENERATION_REFUSALS.labels(category or "none").inc()


def count_security_refusal(reason: str, operation: str | None = None) -> None:
    SECURITY_REFUSALS.labels(reason).inc()
    if operation is not None:
        RATE_LIMITED.labels(operation).inc()


class _Snapshot:
    """A collector for metrics read at scrape time, such as the queue's state."""

    def __init__(self, families: Iterable[Metric]) -> None:
        self._families = list(families)

    def collect(self) -> Iterator[Metric]:
        return iter(self._families)


def exposition(state: Iterable[Metric] = ()) -> bytes:
    """Everything in the Prometheus text format: this process's metrics, then ``state``."""
    snapshot = CollectorRegistry(auto_describe=False)
    snapshot.register(_Snapshot(state))
    return generate_latest(REGISTRY) + generate_latest(snapshot)
