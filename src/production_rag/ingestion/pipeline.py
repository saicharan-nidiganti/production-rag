"""Ingestion (FR-3): accept -> queue -> claim -> extract, chunk, embed -> ready or failed.

``submit`` runs the checks that need no parsing, stores the upload and queues the document,
then returns. Workers (``worker.py``) claim queued documents from the queue (``queue.py``,
which owns the claim protocol) and ``process`` them. Chunks are written in the same
transaction that marks a document ready, so a failed or interrupted document never has
chunks (NFR-5).

``ingest`` does all of it in the calling thread, for the CLI and the evaluation runner.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from production_rag.config import Settings
from production_rag.db.documents import DocumentRepository
from production_rag.db.models import Chunk, Document, DocumentFile, utcnow
from production_rag.embeddings import Embedder, to_blob
from production_rag.ingestion.chunking import ChunkSpan, chunk_document
from production_rag.ingestion.queue import Claim, ClaimLost, IngestionQueue
from production_rag.parsing.extract import (
    ExtractionError,
    UnsupportedDocumentError,
    content_type_for,
    safe_filename,
    validate_upload,
)
from production_rag.parsing.isolation import ParseFailedError, ParseTimeoutError, parse

logger = logging.getLogger(__name__)

_EMBED_BATCH = 64  # chunks embedded between lease renewals


class DocumentTooLargeError(ValueError):
    pass


class DocumentStateError(ValueError):
    """The requested change doesn't apply to the document's current status."""


class UploadMissingError(RuntimeError):
    pass


class _Stopping(Exception):
    """The worker is shutting down; the document goes back to the queue."""


@dataclass(frozen=True)
class IngestResult:
    document: Document
    created: bool  # False when identical content was already submitted


@dataclass(frozen=True)
class ProcessOutcome:
    status: Literal["ready", "failed", "released", "lost"]
    error: Exception | None = None


# Expected failures and their codes; the message is the exception's, written for users.
_FAILURE_CODES: tuple[tuple[type[Exception], str], ...] = (
    (ExtractionError, "extraction_failed"),
    (UnsupportedDocumentError, "unsupported_document_type"),
    (UploadMissingError, "upload_missing"),
    (ParseTimeoutError, "parse_timeout"),
    (ParseFailedError, "parse_failed"),
)


def _failure_reason(exc: Exception) -> tuple[str, str, bool]:
    """(code, message, expected). Unexpected errors don't expose their message."""
    for kind, code in _FAILURE_CODES:
        if isinstance(exc, kind):
            return code, str(exc), True
    message = (
        f"processing failed unexpectedly ({type(exc).__name__}); the server log has the "
        "details. Retry once the cause is fixed"
    )
    return "internal_error", message, False


def embedding_input(chunk_text: str, heading: str | None) -> str:
    """Text that gets embedded for a chunk: the heading path gives each chunk its context."""
    return f"{heading}\n\n{chunk_text}" if heading else chunk_text


class IngestionService:
    def __init__(
        self, sessions: sessionmaker[Session], embedder: Embedder, settings: Settings
    ) -> None:
        self._sessions = sessions
        self._embedder = embedder
        self._settings = settings
        self._documents = DocumentRepository(sessions)
        self.queue = IngestionQueue(sessions, settings)

    # --- Accepting documents ---

    def submit(self, filename: str, data: bytes, *, notify: bool = True) -> IngestResult:
        """Validate and queue a document, without processing it.

        Identical bytes are stored once (FR-4): the existing document is returned instead,
        and queued again if it had failed.
        """
        filename = safe_filename(filename)
        content_type_for(filename)  # reject unsupported types before reading the content
        if len(data) > self._settings.max_upload_bytes:
            raise DocumentTooLargeError(
                f"document is {len(data)} bytes; the limit is {self._settings.max_upload_bytes}"
            )
        content_type = validate_upload(filename, data)
        content_hash = hashlib.sha256(data).hexdigest()

        with self._sessions() as session:
            existing = self._find_by_hash(session, content_hash)
            if existing is None:
                now = utcnow()
                document = Document(
                    source_name=filename,
                    title=PurePosixPath(filename).stem,
                    content_type=content_type,
                    content_hash=content_hash,
                    size_bytes=len(data),
                    text="",
                    metadata_={},
                    num_chunks=0,
                    status="queued",
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                )
                try:
                    session.add(document)
                    session.flush()
                    session.add(DocumentFile(document_id=document.id, data=data))
                    session.commit()
                except IntegrityError:
                    # A concurrent request submitted the same bytes first.
                    session.rollback()
                    existing = self._find_by_hash(session, content_hash)
                    if existing is None:
                        raise
                else:
                    logger.info(
                        "ingest.queued",
                        extra={
                            "document_id": document.id,
                            "content_type": content_type,
                            "size_bytes": len(data),
                        },
                    )
                    if notify:
                        self.queue.work_available.set()
                    return IngestResult(document, created=True)

            if existing.status == "failed" and self.queue.requeue_failed(session, existing.id):
                if session.get(DocumentFile, existing.id) is None:
                    session.add(DocumentFile(document_id=existing.id, data=data))
                session.commit()
                session.refresh(existing)
                logger.info(
                    "ingest.requeued",
                    extra={"document_id": existing.id, "reason": "duplicate_upload"},
                )
                if notify:
                    self.queue.work_available.set()
            else:
                logger.info(
                    "ingest.duplicate",
                    extra={"document_id": existing.id, "status": existing.status},
                )
            return IngestResult(existing, created=False)

    def retry(self, document_id: str) -> Document | None:
        """Queue a failed document again; None when it doesn't exist."""
        with self._sessions() as session:
            document = session.get(Document, document_id)
            if document is None:
                return None
            if document.status != "failed" or not self.queue.requeue_failed(session, document_id):
                raise DocumentStateError(
                    f"only failed documents can be retried; this one is {document.status}"
                )
            session.commit()
            session.refresh(document)
        logger.info("ingest.requeued", extra={"document_id": document_id, "reason": "retry"})
        self.queue.work_available.set()
        return document

    def ingest(self, filename: str, data: bytes) -> IngestResult:
        """Submit and process a document in the calling thread (CLI and evaluation).

        Raises the processing error when processing fails; the document stays, as failed.
        """
        submitted = self.submit(filename, data, notify=False)
        if submitted.document.status != "queued":
            return submitted
        claim = self.queue.claim(submitted.document.id)
        if claim is not None:  # None: a worker elsewhere took it first
            outcome = self.process(claim)
            if outcome.error is not None:
                raise outcome.error
        return IngestResult(
            self._documents.get(submitted.document.id) or submitted.document, submitted.created
        )

    # --- Processing ---

    def process(
        self, claim: Claim, *, should_stop: Callable[[], bool] = lambda: False
    ) -> ProcessOutcome:
        """Extract, chunk and embed a claimed document, then mark it ready or failed."""
        start = time.perf_counter()
        log = {"document_id": claim.document_id, "attempt": claim.attempt}
        logger.info("ingest.started", extra=log)
        try:
            with self._sessions() as session:
                document = session.get(Document, claim.document_id)
                upload = session.get(DocumentFile, claim.document_id)
                if document is None:
                    raise ClaimLost
                filename = document.source_name
                data = upload.data if upload is not None else None
            if data is None:
                raise UploadMissingError(
                    "the uploaded file is no longer stored; upload the document again"
                )
            extracted = parse(filename, data, self._settings)
            spans = chunk_document(
                extracted.text,
                extracted.sections,
                chunk_size=self._settings.chunk_size,
                overlap=self._settings.chunk_overlap,
            )
            if not spans:
                raise ExtractionError("document contains no extractable text")
            vectors: list[bytes] = []
            for i in range(0, len(spans), _EMBED_BATCH):
                # Each batch is a checkpoint: stop if asked to, otherwise renew the lease.
                if should_stop():
                    raise _Stopping
                self.queue.renew(claim)
                batch = spans[i : i + _EMBED_BATCH]
                embedded = self._embedder.embed_documents(
                    [embedding_input(span.text, span.heading) for span in batch]
                )
                vectors.extend(to_blob(vector) for vector in embedded)
            self.queue.complete(
                claim,
                [self._chunk(claim.document_id, i, s, vectors[i]) for i, s in enumerate(spans)],
                title=extracted.title,
                content_type=extracted.content_type,
                text=extracted.text,
                metadata_=extracted.metadata,
                num_chunks=len(spans),
            )
        except ClaimLost:
            logger.warning("ingest.claim_lost", extra=log)
            return ProcessOutcome("lost")
        except _Stopping:
            self.queue.release(claim)
            logger.info("ingest.released", extra=log)
            return ProcessOutcome("released")
        except Exception as exc:
            code, message, expected = _failure_reason(exc)
            self.queue.fail(claim, code, message)
            logger.log(
                logging.WARNING if expected else logging.ERROR,
                "ingest.failed",
                extra={**log, "error_code": code},
                exc_info=None if expected else exc,
            )
            return ProcessOutcome("failed", exc)
        logger.info(
            "ingest.completed",
            extra={
                **log,
                "content_type": extracted.content_type,
                "size_bytes": len(data),
                "chunks": len(spans),
                "embedding_model": self._embedder.model_id,
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
            },
        )
        return ProcessOutcome("ready")

    # --- Maintenance ---

    def reindex(self, batch_size: int = 256) -> int:
        """Re-embed chunks whose embedding model differs from the configured one."""
        updated = 0
        with self._sessions() as session:
            while True:
                batch = session.scalars(
                    select(Chunk)
                    .where(Chunk.embedding_model != self._embedder.model_id)
                    .order_by(Chunk.id)
                    .limit(batch_size)
                ).all()
                if not batch:
                    break
                vectors = self._embedder.embed_documents(
                    [embedding_input(c.text, c.heading) for c in batch]
                )
                for chunk, vector in zip(batch, vectors, strict=True):
                    chunk.embedding = to_blob(vector)
                    chunk.embedding_model = self._embedder.model_id
                session.commit()
                updated += len(batch)
        logger.info("reindex.completed", extra={"chunks": updated})
        return updated

    def _chunk(self, document_id: str, ordinal: int, span: ChunkSpan, embedding: bytes) -> Chunk:
        return Chunk(
            document_id=document_id,
            ordinal=ordinal,
            text=span.text,
            start_char=span.start,
            end_char=span.end,
            heading=span.heading,
            page=span.page,
            embedding=embedding,
            embedding_model=self._embedder.model_id,
        )

    @staticmethod
    def _find_by_hash(session: Session, content_hash: str) -> Document | None:
        return session.scalar(select(Document).where(Document.content_hash == content_hash))
