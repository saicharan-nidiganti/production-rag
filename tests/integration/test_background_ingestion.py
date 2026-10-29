"""Background ingestion (FR-3): states, failures, retries, recovery, restarts, concurrency."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence

import numpy as np
import pytest
from sqlalchemy import select, text, update

from production_rag.config import Settings
from production_rag.container import Container, build_container
from production_rag.db.models import Chunk, Document, DocumentFile, utcnow
from production_rag.embeddings import Embedder, Vector
from production_rag.ingestion import (
    DocumentStateError,
    ExtractionError,
    IngestionService,
    IngestionWorker,
)

GUIDE = b"""# Guide

## Install

Run the installer and accept the defaults.

## Rollback

Use the zebracorn command to undo a broken release.
"""
# Passes the upload checks (UTF-8, not blank) but has no text to index.
HEADINGS_ONLY = b"# Title\n\n## Nothing here\n"


def _doc(container: Container, document_id: str) -> Document:
    document = container.ingestion.get(document_id)
    assert document is not None
    return document


def _count(container: Container, sql: str) -> int:
    with container.engine.connect() as conn:
        return int(conn.execute(text(sql)).scalar() or 0)


def _expire_lease(container: Container, document_id: str) -> None:
    """What a crashed worker leaves behind once its lease has lapsed."""
    with container.sessions() as session:
        session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(lease_expires_at=utcnow().replace(year=2000))
        )
        session.commit()


def _wait_until(condition: Callable[[], bool], timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


class _FailingOnceEmbedder:
    """Raises on the first call, as a model download or a full disk would, then works."""

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner
        self.calls = 0

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    def embed_documents(self, texts: Sequence[str]) -> Vector:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("disk full at /secret/path")
        return self._inner.embed_documents(texts)

    def embed_query(self, text_: str) -> Vector:
        return self._inner.embed_query(text_)


def test_submit_queues_the_document_and_keeps_the_upload(container: Container) -> None:
    result = container.ingestion.submit("guide.md", GUIDE)
    doc = result.document
    assert result.created
    assert (doc.status, doc.attempts, doc.num_chunks, doc.title) == ("queued", 0, 0, "guide")
    assert doc.error_code is None and doc.claim_token is None
    with container.sessions() as session:
        upload = session.get(DocumentFile, doc.id)
        assert upload is not None and upload.data == GUIDE
    assert _count(container, "SELECT count(*) FROM chunks") == 0
    assert container.ingestion.status_counts() == {
        "queued": 1,
        "processing": 0,
        "ready": 0,
        "failed": 0,
    }
    assert container.ingestion.work_available.is_set()  # wakes this process's workers


def test_a_document_moves_from_queued_to_processing_to_ready(container: Container) -> None:
    doc = container.ingestion.submit("guide.md", GUIDE).document
    claim = container.ingestion.claim_next()
    assert claim is not None and (claim.document_id, claim.attempt) == (doc.id, 1)

    processing = _doc(container, doc.id)
    assert processing.status == "processing" and processing.attempts == 1
    assert processing.claim_token == claim.token
    assert processing.lease_expires_at is not None
    assert container.ingestion.claim_next() is None  # a live claim is never taken over
    # Not searchable until ready.
    assert container.rag.search("zebracorn", top_k=3, mode="lexical").chunks == []

    assert container.ingestion.process(claim).status == "ready"
    ready = _doc(container, doc.id)
    assert (ready.status, ready.title, ready.num_chunks) == ("ready", "Guide", 2)
    assert ready.claim_token is None and ready.lease_expires_at is None
    assert ready.error_code is None and ready.updated_at >= processing.updated_at
    assert _count(container, "SELECT count(*) FROM document_files") == 0  # upload discarded
    assert container.rag.search("zebracorn", top_k=3, mode="lexical").chunks


def test_a_failure_records_an_actionable_reason_and_keeps_the_upload(
    container: Container,
) -> None:
    doc = container.ingestion.submit("headings.md", HEADINGS_ONLY).document
    claim = container.ingestion.claim_next()
    assert claim is not None
    outcome = container.ingestion.process(claim)
    assert outcome.status == "failed" and isinstance(outcome.error, ExtractionError)

    failed = _doc(container, doc.id)
    assert failed.status == "failed"
    assert failed.error_code == "extraction_failed"
    assert failed.error_message == "document contains no extractable text"
    assert failed.claim_token is None and failed.num_chunks == 0
    assert _count(container, "SELECT count(*) FROM chunks") == 0  # nothing partial (NFR-5)
    assert _count(container, "SELECT count(*) FROM document_files") == 1  # kept for a retry


def test_a_broken_pdf_fails_in_the_background(container: Container) -> None:
    doc = container.ingestion.submit("broken.pdf", b"%PDF-1.7\nnot really a pdf").document
    claim = container.ingestion.claim_next()
    assert claim is not None
    assert container.ingestion.process(claim).status == "failed"
    failed = _doc(container, doc.id)
    assert failed.error_code == "extraction_failed"
    assert (failed.error_message or "").startswith("could not read PDF")


def test_synchronous_ingest_raises_and_leaves_a_failed_document(container: Container) -> None:
    with pytest.raises(ExtractionError):
        container.ingestion.ingest("headings.md", HEADINGS_ONLY)
    assert container.ingestion.status_counts()["failed"] == 1


def test_unexpected_errors_hide_their_details_and_recover_on_retry(
    container: Container, caplog: pytest.LogCaptureFixture
) -> None:
    embedder = _FailingOnceEmbedder(container.embedder)
    service = IngestionService(container.sessions, embedder, container.settings)
    doc = service.submit("guide.md", GUIDE).document
    claim = service.claim_next()
    assert claim is not None
    with caplog.at_level(logging.ERROR):
        assert service.process(claim).status == "failed"
    failed = _doc(container, doc.id)
    assert failed.error_code == "internal_error"
    assert "RuntimeError" in (failed.error_message or "")
    assert "/secret/path" not in (failed.error_message or "")  # details stay in the log
    assert any(r.message == "ingest.failed" and r.exc_info for r in caplog.records)

    requeued = service.retry(doc.id)
    assert requeued is not None
    assert (requeued.status, requeued.attempts, requeued.error_code) == ("queued", 0, None)
    claim = service.claim_next()
    assert claim is not None and claim.attempt == 1
    assert service.process(claim).status == "ready"
    assert _doc(container, doc.id).num_chunks == 2


def test_only_failed_documents_can_be_retried(container: Container) -> None:
    assert container.ingestion.retry("no-such-id") is None
    doc = container.ingestion.submit("guide.md", GUIDE).document
    with pytest.raises(DocumentStateError, match="this one is queued"):
        container.ingestion.retry(doc.id)


def test_duplicates_return_the_existing_document_in_every_state(container: Container) -> None:
    first = container.ingestion.submit("guide.md", GUIDE).document
    queued_again = container.ingestion.submit("copy.md", GUIDE)
    assert not queued_again.created and queued_again.document.id == first.id
    assert queued_again.document.status == "queued"
    assert _count(container, "SELECT count(*) FROM documents") == 1
    assert _count(container, "SELECT count(*) FROM document_files") == 1

    claim = container.ingestion.claim_next()
    assert claim is not None
    during = container.ingestion.submit("copy.md", GUIDE)
    assert during.document.status == "processing" and not during.created
    container.ingestion.process(claim)
    after = container.ingestion.submit("copy.md", GUIDE)
    assert after.document.status == "ready" and after.document.source_name == "guide.md"


def test_reuploading_a_failed_document_queues_it_again(container: Container) -> None:
    doc = container.ingestion.submit("headings.md", HEADINGS_ONLY).document
    claim = container.ingestion.claim_next()
    assert claim is not None
    container.ingestion.process(claim)
    again = container.ingestion.submit("headings.md", HEADINGS_ONLY)
    assert not again.created and again.document.id == doc.id
    assert (again.document.status, again.document.attempts) == ("queued", 0)
    assert again.document.error_code is None


def test_a_crashed_workers_document_is_taken_over_once_its_lease_lapses(
    container: Container,
) -> None:
    doc = container.ingestion.submit("guide.md", GUIDE).document
    crashed = container.ingestion.claim_next()
    assert crashed is not None
    _expire_lease(container, doc.id)

    takeover = container.ingestion.claim_next()
    assert takeover is not None and takeover.document_id == doc.id
    assert takeover.attempt == 2 and takeover.token != crashed.token
    # The first worker wasn't dead after all and finishes late: its work is discarded.
    assert container.ingestion.process(crashed).status == "lost"
    assert container.ingestion.process(takeover).status == "ready"
    assert _count(container, "SELECT count(*) FROM chunks") == 2  # not 4


def test_repeated_interruptions_fail_the_document_until_retried(container: Container) -> None:
    container.settings.ingest_max_attempts = 2
    doc = container.ingestion.submit("guide.md", GUIDE).document
    for _ in range(2):
        assert container.ingestion.claim_next() is not None
        _expire_lease(container, doc.id)
    assert container.ingestion.claim_next() is None  # gives up instead of a third try

    failed = _doc(container, doc.id)
    assert failed.status == "failed" and failed.error_code == "interrupted"
    assert "interrupted 2 times" in (failed.error_message or "")
    container.ingestion.retry(doc.id)
    claim = container.ingestion.claim_next()
    assert claim is not None and container.ingestion.process(claim).status == "ready"


def test_stopping_puts_the_document_back_without_counting_the_attempt(
    container: Container,
) -> None:
    doc = container.ingestion.submit("guide.md", GUIDE).document
    claim = container.ingestion.claim_next()
    assert claim is not None
    assert container.ingestion.process(claim, should_stop=lambda: True).status == "released"
    released = _doc(container, doc.id)
    assert (released.status, released.attempts, released.claim_token) == ("queued", 0, None)
    assert _count(container, "SELECT count(*) FROM chunks") == 0


def test_deleting_a_document_while_it_is_processed_discards_the_work(
    container: Container,
) -> None:
    doc = container.ingestion.submit("guide.md", GUIDE).document
    claim = container.ingestion.claim_next()
    assert claim is not None
    with container.sessions() as session:
        session.delete(session.get(Document, doc.id))
        session.commit()
    assert container.ingestion.process(claim).status == "lost"
    assert _count(container, "SELECT count(*) FROM chunks") == 0
    assert _count(container, "SELECT count(*) FROM document_files") == 0  # cascaded


def test_a_missing_upload_fails_with_its_own_code(container: Container) -> None:
    doc = container.ingestion.submit("guide.md", GUIDE).document
    with container.sessions() as session:
        session.delete(session.get(DocumentFile, doc.id))
        session.commit()
    claim = container.ingestion.claim_next()
    assert claim is not None
    assert container.ingestion.process(claim).status == "failed"
    assert _doc(container, doc.id).error_code == "upload_missing"


def test_queued_and_interrupted_documents_survive_a_restart(migrated_settings: Settings) -> None:
    before = build_container(migrated_settings)
    queued = before.ingestion.submit("guide.md", GUIDE).document
    other = before.ingestion.submit("other.md", b"# Other\n\nA different document.\n").document
    stuck = before.ingestion.claim_next()  # the process dies while processing this one
    assert stuck is not None and stuck.document_id == queued.id
    before.close()

    restarted = build_container(migrated_settings)
    worker = IngestionWorker(restarted.ingestion, threads=1, poll_interval_s=0.02)
    try:
        assert restarted.ingestion.status_counts() == {
            "queued": 1,
            "processing": 1,
            "ready": 0,
            "failed": 0,
        }
        _expire_lease(restarted, queued.id)
        worker.start()
        _wait_until(lambda: restarted.ingestion.status_counts()["ready"] == 2)
        worker.stop()
        assert _doc(restarted, queued.id).attempts == 2  # taken over
        assert _doc(restarted, other.id).attempts == 1
        assert restarted.rag.search("zebracorn", top_k=1, mode="lexical").chunks
    finally:
        worker.stop()
        restarted.close()


def test_a_worker_keeps_going_after_a_failing_document(container: Container) -> None:
    bad = container.ingestion.submit("headings.md", HEADINGS_ONLY).document
    good = container.ingestion.submit("guide.md", GUIDE).document
    worker = IngestionWorker(container.ingestion, threads=1, poll_interval_s=0.02)
    worker.start()
    try:
        _wait_until(lambda: _doc(container, good.id).status == "ready")
        assert _doc(container, bad.id).status == "failed"
        assert worker.alive == 1
    finally:
        worker.stop()
    assert worker.alive == 0


def test_workers_in_two_processes_process_each_document_exactly_once(
    migrated_settings: Settings,
) -> None:
    containers = [build_container(migrated_settings) for _ in range(2)]
    workers = [IngestionWorker(c.ingestion, threads=2, poll_interval_s=0.01) for c in containers]
    try:
        ids = [
            containers[0]
            .ingestion.submit(f"doc-{i}.md", f"# Doc {i}\n\nUnique word marker{i}.\n".encode())
            .document.id
            for i in range(8)
        ]
        for worker in workers:
            worker.start()
        _wait_until(lambda: containers[1].ingestion.status_counts()["ready"] == len(ids))
        for worker in workers:
            worker.stop()
        with containers[0].sessions() as session:
            attempts = session.scalars(select(Document.attempts)).all()
            chunks = session.scalars(select(Chunk.document_id)).all()
        assert attempts == [1] * len(ids)
        assert sorted(chunks) == sorted(ids)  # one chunk each, no duplicates
    finally:
        for worker in workers:
            worker.stop()
        for c in containers:
            c.close()


def test_a_worker_backs_off_while_the_schema_is_missing(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    unmigrated = build_container(settings)
    worker = IngestionWorker(unmigrated.ingestion, threads=1, poll_interval_s=0.01)
    try:
        with caplog.at_level(logging.ERROR):
            worker.start()
            _wait_until(lambda: any(r.message == "ingest.worker_error" for r in caplog.records))
        assert worker.alive == 1  # still running, retrying with a backoff
    finally:
        worker.stop()
        unmigrated.close()
    assert worker.alive == 0


def test_embedding_runs_in_batches_with_lease_renewals(container: Container) -> None:
    calls: list[int] = []

    class CountingEmbedder:
        model_id = container.embedder.model_id

        def embed_documents(self, texts: Sequence[str]) -> Vector:
            calls.append(len(texts))
            return np.zeros((len(texts), 8), dtype=np.float32)

        def embed_query(self, text_: str) -> Vector:
            return np.zeros(8, dtype=np.float32)

    container.settings.chunk_size, container.settings.chunk_overlap = 100, 0
    body = "\n\n".join(f"Paragraph {i} " + "word " * 15 for i in range(150))
    service = IngestionService(container.sessions, CountingEmbedder(), container.settings)
    doc = service.submit("long.txt", body.encode()).document
    renewals: list[bool] = []

    def should_stop() -> bool:
        renewals.append(True)
        return False

    claim = service.claim_next()
    assert claim is not None
    outcome = service.process(claim, should_stop=should_stop)
    assert outcome.status == "ready"
    assert len(calls) > 1 and max(calls) <= 64
    assert len(renewals) == len(calls)  # renewed (and checked for shutdown) before each batch
    assert _doc(container, doc.id).num_chunks == sum(calls)


def test_a_takeover_after_the_last_renewal_is_caught_when_completing(
    container: Container,
) -> None:
    """The completion itself checks the claim, not only the lease renewals before it."""
    service: IngestionService

    class TakenOverWhileEmbedding:
        model_id = container.embedder.model_id

        def __init__(self) -> None:
            self.takeover: object = None

        def embed_documents(self, texts: Sequence[str]) -> Vector:
            if self.takeover is None:  # the lease lapses and another worker claims it
                _expire_lease(container, doc.id)
                self.takeover = service.claim_next()
            return container.embedder.embed_documents(texts)

        def embed_query(self, text_: str) -> Vector:
            return container.embedder.embed_query(text_)

    embedder = TakenOverWhileEmbedding()
    service = IngestionService(container.sessions, embedder, container.settings)
    doc = service.submit("guide.md", GUIDE).document
    first = service.claim_next()
    assert first is not None
    assert service.process(first).status == "lost"
    assert embedder.takeover is not None
    assert _count(container, "SELECT count(*) FROM chunks") == 0
    assert _doc(container, doc.id).status == "processing"  # the new owner's claim stands


def test_a_concurrent_identical_upload_returns_the_winner(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = container.ingestion.submit("guide.md", GUIDE).document
    # The second request looked before the first committed, so it tries to insert too.
    lookups = iter([None])
    original = IngestionService._find_by_hash
    monkeypatch.setattr(
        IngestionService,
        "_find_by_hash",
        staticmethod(lambda session, h: next(lookups, original(session, h))),
    )
    second = container.ingestion.submit("copy.md", GUIDE)
    assert not second.created and second.document.id == first.id
    assert _count(container, "SELECT count(*) FROM documents") == 1


def test_a_failed_lease_renewal_does_not_fail_the_document(
    container: Container, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from sqlalchemy.exc import OperationalError

    original = IngestionService._set

    def locked_on_renewal(session, document_id, conditions, **values):  # type: ignore[no-untyped-def]
        if set(values) == {"lease_expires_at"}:
            raise OperationalError("UPDATE documents", {}, Exception("database is locked"))
        return original(session, document_id, conditions, **values)

    monkeypatch.setattr(IngestionService, "_set", staticmethod(locked_on_renewal))
    doc = container.ingestion.submit("guide.md", GUIDE).document
    claim = container.ingestion.claim_next()
    assert claim is not None
    with caplog.at_level(logging.WARNING):
        assert container.ingestion.process(claim).status == "ready"
    assert any(r.message == "ingest.lease_renewal_failed" for r in caplog.records)
    assert _doc(container, doc.id).status == "ready"


def test_uploading_again_restores_a_missing_upload(container: Container) -> None:
    doc = container.ingestion.submit("guide.md", GUIDE).document
    with container.sessions() as session:
        session.delete(session.get(DocumentFile, doc.id))
        session.commit()
    claim = container.ingestion.claim_next()
    assert claim is not None and container.ingestion.process(claim).status == "failed"

    again = container.ingestion.submit("guide.md", GUIDE)
    assert again.document.status == "queued"
    claim = container.ingestion.claim_next()
    assert claim is not None and container.ingestion.process(claim).status == "ready"
