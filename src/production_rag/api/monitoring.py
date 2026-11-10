"""``GET /metrics``: this process's Prometheus metrics, and the state of the index and queue.

The route sits behind the API key like ``/v1`` (Prometheus sends it with
``authorization: {credentials: ...}``) and draws from the ``read`` rate limit. It reads the
database on each scrape, with a handful of indexed counts, and still answers when the
database is down, with ``rag_database_up 0``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.metrics_core import Metric
from sqlalchemy import text

from production_rag import metrics
from production_rag.api.errors import NotFoundError
from production_rag.api.security import require_api_key
from production_rag.container import Container
from production_rag.db.migrate import current_revision, head_revision
from production_rag.ingestion import IngestionWorker

logger = logging.getLogger(__name__)

metrics_router = APIRouter(tags=["monitoring"], dependencies=[Depends(require_api_key)])


@metrics_router.get(
    "/metrics",
    response_class=Response,
    responses={200: {"content": {metrics.CONTENT_TYPE: {}}, "description": "Prometheus text"}},
)
def prometheus_metrics(request: Request) -> Response:
    """Prometheus metrics in the text exposition format (docs/observability.md)."""
    container: Container = request.app.state.container
    if not container.settings.metrics_enabled:
        raise NotFoundError("metrics are turned off (RAG_METRICS_ENABLED=false)")
    worker: IngestionWorker | None = getattr(request.app.state, "ingest_worker", None)
    body = metrics.exposition(_state(container, worker.alive if worker else 0))
    return Response(content=body, media_type=metrics.CONTENT_TYPE)


def _gauge(name: str, documentation: str, value: float) -> GaugeMetricFamily:
    return GaugeMetricFamily(name, documentation, value=value)


def _state(container: Container, workers: int) -> Iterator[Metric]:
    yield _gauge("rag_ingestion_workers", "Live ingestion worker threads in this process.", workers)
    try:
        with container.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        at_head = current_revision(container.engine) == head_revision()
        stats = container.index_stats() if at_head else None
        counts = container.documents.status_counts() if at_head else None
        oldest = container.documents.oldest_queued_at() if at_head else None
    except Exception as exc:  # a scrape must still answer, to report the outage
        logger.warning("metrics.database_unavailable", extra={"error": type(exc).__name__})
        yield _gauge("rag_database_up", "1 if the database answered this scrape.", 0)
        return
    yield _gauge("rag_database_up", "1 if the database answered this scrape.", 1)
    yield _gauge(
        "rag_schema_at_head", "1 if the schema is at the latest migration.", 1 if at_head else 0
    )
    if stats is None or counts is None:
        return  # behind on migrations: the tables may not match the queries
    documents = GaugeMetricFamily(
        "rag_documents", "Documents by ingestion status.", labels=["status"]
    )
    for status, count in sorted(counts.items()):
        documents.add_metric([status], count)
    yield documents
    yield _gauge("rag_chunks", "Chunks in the index.", stats["chunks"])
    yield _gauge(
        "rag_stale_chunks",
        "Chunks embedded by a model other than the configured one; `rag reindex` fixes them.",
        stats["stale_chunks"],
    )
    age = (datetime.now(UTC) - oldest).total_seconds() if oldest else 0.0
    yield _gauge(
        "rag_ingestion_oldest_queued_seconds",
        "How long the longest-waiting queued document has waited; 0 when none waits.",
        max(age, 0.0),
    )
