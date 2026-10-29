"""Ingestion (FR-3): accept -> queue -> claim -> extract, chunk, embed -> ready or failed.

``submit`` runs the checks that need no parsing, stores the upload and queues the document,
then returns. Workers (``worker.py``) claim queued documents and ``process`` them.
A claim is a compare-and-set on the document row, so threads and processes can share the
database, and it carries a lease that the worker renews as it goes. If a worker stops
(crash, restart), its lease lapses and another worker takes the document over; a worker
that has lost its claim discards its work. Chunks are written in the same transaction that
marks the document ready, so a failed or interrupted document never has chunks (NFR-5).

``ingest`` does all of it in the calling thread, for the CLI and the evaluation runner.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Literal, cast, get_args

from sqlalchemy import ColumnElement, CursorResult, and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from production_rag.config import Settings
from production_rag.db.models import Chunk, Document, DocumentFile, DocumentStatus, utcnow
from production_rag.embeddings import Embedder, to_blob
from production_rag.ingestion.chunking import ChunkSpan, chunk_document
from production_rag.ingestion.extract import (
    ExtractedDocument,
    ExtractionError,
    UnsupportedDocumentError,
    content_type_for,
    extract,
    safe_filename,
    validate_upload,
)

logger = logging.getLogger(__name__)

_EMBED_BATCH = 64  # chunks embedded between lease renewals


class DocumentTooLargeError(ValueError):
    pass


class DocumentStateError(ValueError):
    """The requested change doesn't apply to the document's current status."""


class UploadMissingError(RuntimeError):
    pass


class _ClaimLost(Exception):
    """The document was deleted, or another worker took it over, while being processed."""


class _Stopping(Exception):
    """The worker is shutting down; the document goes back to the queue."""


@dataclass(frozen=True)
class IngestResult:
    document: Document
    created: bool  # False when identical content was already submitted


@dataclass(frozen=True)
class Claim:
    document_id: str
    token: str
    attempt: int


@dataclass(frozen=True)
class ProcessOutcome:
    status: Literal["ready", "failed", "released", "lost"]
    error: Exception | None = None


# Expected failures and their codes; the message is the exception's, written for users.
_FAILURE_CODES: tuple[tuple[type[Exception], str], ...] = (
    (ExtractionError, "extraction_failed"),
    (UnsupportedDocumentError, "unsupported_document_type"),
    (UploadMissingError, "upload_missing"),
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


def _token_is(token: str | None) -> ColumnElement[bool]:
    return Document.claim_token.is_(None) if token is None else Document.claim_token == token


class IngestionService:
    def __init__(
        self, sessions: sessionmaker[Session], embedder: Embedder, settings: Settings
    ) -> None:
        self._sessions = sessions
        self._embedder = embedder
        self._settings = settings
        # Set when work is queued by this process, so its idle workers start at once
        # instead of at their next poll. Workers in other processes poll.
        self.work_available = threading.Event()

    @property
    def _lease(self) -> timedelta:
        return timedelta(seconds=self._settings.ingest_lease_s)

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
        now = utcnow()

        with self._sessions() as session:
            existing = self._find_by_hash(session, content_hash)
            if existing is None:
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
                        self.work_available.set()
                    return IngestResult(document, created=True)

            if existing.status == "failed" and self._set(
                session, existing.id, [Document.status == "failed"], **self._requeued(now)
            ):
                if session.get(DocumentFile, existing.id) is None:
                    session.add(DocumentFile(document_id=existing.id, data=data))
                session.commit()
                session.refresh(existing)
                logger.info(
                    "ingest.requeued",
                    extra={"document_id": existing.id, "reason": "duplicate_upload"},
                )
                if notify:
                    self.work_available.set()
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
            if document.status != "failed" or not self._set(
                session, document_id, [Document.status == "failed"], **self._requeued(utcnow())
            ):
                raise DocumentStateError(
                    f"only failed documents can be retried; this one is {document.status}"
                )
            session.commit()
            session.refresh(document)
        logger.info("ingest.requeued", extra={"document_id": document_id, "reason": "retry"})
        self.work_available.set()
        return document

    def ingest(self, filename: str, data: bytes) -> IngestResult:
        """Submit and process a document in the calling thread (CLI and evaluation).

        Raises the processing error when processing fails; the document stays, as failed.
        """
        submitted = self.submit(filename, data, notify=False)
        if submitted.document.status != "queued":
            return submitted
        claim = self._claim_queued(submitted.document.id)
        if claim is not None:  # None: a worker elsewhere took it first
            outcome = self.process(claim)
            if outcome.error is not None:
                raise outcome.error
        return IngestResult(
            self.get(submitted.document.id) or submitted.document, submitted.created
        )

    # --- Claiming and processing ---

    def claim_next(self) -> Claim | None:
        """Claim the oldest queued document, or take over one whose worker has stopped."""
        while True:
            now = utcnow()
            with self._sessions() as session:
                row = session.execute(
                    select(Document.id, Document.status, Document.claim_token, Document.attempts)
                    .where(
                        or_(
                            Document.status == "queued",
                            and_(Document.status == "processing", Document.lease_expires_at < now),
                        )
                    )
                    .order_by(Document.updated_at, Document.id)
                    .limit(1)
                ).first()
                if row is None:
                    return None
                max_attempts = self._settings.ingest_max_attempts
                if row.status == "processing" and row.attempts >= max_attempts:
                    message = (
                        f"processing was interrupted {row.attempts} time"
                        f"{'s' if row.attempts != 1 else ''} (the worker stopped "
                        "before finishing). Retry; if it keeps happening, check the server "
                        "log and resources for this document"
                    )
                    if self._set(
                        session,
                        row.id,
                        [_token_is(row.claim_token)],
                        **self._failed("interrupted", message, now),
                    ):
                        logger.warning(
                            "ingest.failed",
                            extra={
                                "document_id": row.id,
                                "attempt": row.attempts,
                                "error_code": "interrupted",
                            },
                        )
                    session.commit()
                    continue
                claim = self._claim(session, row.id, row.status, row.claim_token, row.attempts)
                session.commit()
            if claim is not None:
                if row.status == "processing":
                    logger.warning(
                        "ingest.recovered",
                        extra={"document_id": claim.document_id, "attempt": claim.attempt},
                    )
                return claim
            # Another worker claimed it between the read and the update: look again.

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
                    raise _ClaimLost
                filename = document.source_name
                data = upload.data if upload is not None else None
            if data is None:
                raise UploadMissingError(
                    "the uploaded file is no longer stored; upload the document again"
                )
            extracted = extract(filename, data, max_pdf_pages=self._settings.max_pdf_pages)
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
                self._renew(claim, should_stop)
                batch = spans[i : i + _EMBED_BATCH]
                embedded = self._embedder.embed_documents(
                    [embedding_input(span.text, span.heading) for span in batch]
                )
                vectors.extend(to_blob(vector) for vector in embedded)
            self._complete(claim, extracted, spans, vectors)
        except _ClaimLost:
            logger.warning("ingest.claim_lost", extra=log)
            return ProcessOutcome("lost")
        except _Stopping:
            self._release(claim)
            logger.info("ingest.released", extra=log)
            return ProcessOutcome("released")
        except Exception as exc:
            code, message, expected = _failure_reason(exc)
            self._fail(claim, code, message)
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

    # --- Reading state ---

    def get(self, document_id: str) -> Document | None:
        with self._sessions() as session:
            return session.get(Document, document_id)

    def status_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(get_args(DocumentStatus), 0)
        with self._sessions() as session:
            rows = session.execute(
                select(Document.status, func.count()).group_by(Document.status)
            ).all()
        counts.update(dict(rows))
        return counts

    def index_stats(self) -> dict[str, int]:
        """Document and chunk counts, and chunks embedded by a model other than the current one."""
        with self._sessions() as session:
            documents = session.scalar(select(func.count()).select_from(Document)) or 0
            chunks = session.scalar(select(func.count()).select_from(Chunk)) or 0
            stale = (
                session.scalar(
                    select(func.count())
                    .select_from(Chunk)
                    .where(Chunk.embedding_model != self._embedder.model_id)
                )
                or 0
            )
        return {"documents": documents, "chunks": chunks, "stale_chunks": stale}

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

    # --- State transitions: each is a compare-and-set on the document row ---

    @staticmethod
    def _set(
        session: Session,
        document_id: str,
        conditions: Sequence[ColumnElement[bool]],
        **values: Any,
    ) -> bool:
        result = cast(
            "CursorResult[Any]",
            session.execute(
                update(Document)
                .where(Document.id == document_id, *conditions)
                .values(**values)
                .execution_options(synchronize_session=False)
            ),
        )
        return result.rowcount == 1

    def _requeued(self, now: datetime) -> dict[str, Any]:
        return {
            "status": "queued",
            "error_code": None,
            "error_message": None,
            "attempts": 0,
            "claim_token": None,
            "lease_expires_at": None,
            "updated_at": now,
        }

    @staticmethod
    def _failed(code: str, message: str, now: datetime) -> dict[str, Any]:
        return {
            "status": "failed",
            "error_code": code,
            "error_message": message,
            "claim_token": None,
            "lease_expires_at": None,
            "updated_at": now,
        }

    def _claim(
        self,
        session: Session,
        document_id: str,
        status: str,
        token: str | None,
        attempts: int,
    ) -> Claim | None:
        now = utcnow()
        new_token = str(uuid.uuid4())
        claimed = self._set(
            session,
            document_id,
            [Document.status == status, _token_is(token)],
            status="processing",
            claim_token=new_token,
            attempts=attempts + 1,
            lease_expires_at=now + self._lease,
            error_code=None,
            error_message=None,
            updated_at=now,
        )
        return Claim(document_id, new_token, attempts + 1) if claimed else None

    def _claim_queued(self, document_id: str) -> Claim | None:
        with self._sessions() as session:
            attempts = session.scalar(
                select(Document.attempts).where(
                    Document.id == document_id, Document.status == "queued"
                )
            )
            if attempts is None:
                return None
            claim = self._claim(session, document_id, "queued", None, attempts)
            session.commit()
        return claim

    def _renew(self, claim: Claim, should_stop: Callable[[], bool]) -> None:
        if should_stop():
            raise _Stopping
        try:
            with self._sessions() as session:
                renewed = self._set(
                    session,
                    claim.document_id,
                    [Document.claim_token == claim.token],
                    lease_expires_at=utcnow() + self._lease,
                )
                session.commit()
        except OperationalError:
            # Usually "database is locked". Skipping one renewal is safe: if the lease lapses
            # and another worker takes over, this worker's completion is discarded.
            logger.warning("ingest.lease_renewal_failed", extra={"document_id": claim.document_id})
            return
        if not renewed:
            raise _ClaimLost

    def _complete(
        self,
        claim: Claim,
        extracted: ExtractedDocument,
        spans: list[ChunkSpan],
        vectors: list[bytes],
    ) -> None:
        with self._sessions() as session:
            if not self._set(
                session,
                claim.document_id,
                [Document.claim_token == claim.token],
                status="ready",
                title=extracted.title,
                content_type=extracted.content_type,
                text=extracted.text,
                metadata_=extracted.metadata,
                num_chunks=len(spans),
                claim_token=None,
                lease_expires_at=None,
                updated_at=utcnow(),
            ):
                raise _ClaimLost
            session.add_all(
                self._chunk(claim.document_id, i, span, vectors[i]) for i, span in enumerate(spans)
            )
            session.execute(
                delete(DocumentFile).where(DocumentFile.document_id == claim.document_id)
            )
            session.commit()

    def _fail(self, claim: Claim, code: str, message: str) -> None:
        with self._sessions() as session:
            self._set(
                session,
                claim.document_id,
                [Document.claim_token == claim.token],
                **self._failed(code, message, utcnow()),
            )
            session.commit()

    def _release(self, claim: Claim) -> None:
        """Back to the queue, without counting the attempt: the worker chose to stop."""
        with self._sessions() as session:
            self._set(
                session,
                claim.document_id,
                [Document.claim_token == claim.token],
                status="queued",
                attempts=Document.attempts - 1,
                claim_token=None,
                lease_expires_at=None,
                updated_at=utcnow(),
            )
            session.commit()

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
