"""Ingestion: validate -> extract -> chunk -> embed -> persist, idempotent by content hash."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from production_rag.config import Settings
from production_rag.db.models import Chunk, Document
from production_rag.embeddings import Embedder, to_blob
from production_rag.ingestion.chunking import ChunkSpan, chunk_document
from production_rag.ingestion.extract import (
    ExtractionError,
    content_type_for,
    extract,
    safe_filename,
)

logger = logging.getLogger(__name__)


class DocumentTooLargeError(ValueError):
    pass


@dataclass(frozen=True)
class IngestResult:
    document: Document
    created: bool  # False when an identical document was already ingested


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

    def ingest(self, filename: str, data: bytes) -> IngestResult:
        filename = safe_filename(filename)
        content_type_for(filename)  # reject unsupported types before doing any work
        if len(data) > self._settings.max_upload_bytes:
            raise DocumentTooLargeError(
                f"document is {len(data)} bytes; the limit is {self._settings.max_upload_bytes}"
            )
        content_hash = hashlib.sha256(data).hexdigest()

        with self._sessions() as session:
            existing = self._find_by_hash(session, content_hash)
            if existing is not None:
                logger.info("ingest.duplicate", extra={"document_id": existing.id})
                return IngestResult(existing, created=False)

        extracted = extract(filename, data, max_pdf_pages=self._settings.max_pdf_pages)
        spans = chunk_document(
            extracted.text,
            extracted.sections,
            chunk_size=self._settings.chunk_size,
            overlap=self._settings.chunk_overlap,
        )
        if not spans:
            raise ExtractionError("document contains no extractable text")
        vectors = self._embedder.embed_documents(
            [embedding_input(span.text, span.heading) for span in spans]
        )

        document = Document(
            source_name=filename,
            title=extracted.title,
            content_type=extracted.content_type,
            content_hash=content_hash,
            size_bytes=len(data),
            text=extracted.text,
            metadata_=extracted.metadata,
            num_chunks=len(spans),
            chunks=[self._chunk(i, span, to_blob(vectors[i])) for i, span in enumerate(spans)],
        )
        with self._sessions() as session:
            session.add(document)
            try:
                session.commit()
            except IntegrityError:
                # A concurrent request ingested the same bytes first.
                session.rollback()
                existing = self._find_by_hash(session, content_hash)
                if existing is None:
                    raise
                return IngestResult(existing, created=False)

        logger.info(
            "ingest.completed",
            extra={
                "document_id": document.id,
                "content_type": document.content_type,
                "size_bytes": document.size_bytes,
                "chunks": len(spans),
                "embedding_model": self._embedder.model_id,
            },
        )
        return IngestResult(document, created=True)

    def _chunk(self, ordinal: int, span: ChunkSpan, embedding: bytes) -> Chunk:
        return Chunk(
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
