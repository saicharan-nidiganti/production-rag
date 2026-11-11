"""Queries over stored documents, for the API, the CLI and ingestion.

Writes that change a document's processing state go through ``ingestion.queue`` instead,
because they must respect the claim protocol.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import get_args

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from production_rag.db.models import Chunk, Document, DocumentStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocumentPage:
    items: list[Document]
    total: int  # all documents matching the filter, not just this page


class DocumentRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    # ``owner`` scopes a call to one owner's documents; ``None`` means every owner's, which
    # is for operator tools (the CLI, the workers). API routes always pass the caller's.

    def get(self, document_id: str, owner: str | None = None) -> Document | None:
        with self._sessions() as session:
            document = session.get(Document, document_id)
        if document is None or (owner is not None and document.owner_id != owner):
            return None  # someone else's document looks exactly like one that doesn't exist
        return document

    def list_documents(
        self,
        *,
        limit: int,
        offset: int,
        status: DocumentStatus | None = None,
        owner: str | None = None,
    ) -> DocumentPage:
        """Newest first."""
        where = [Document.status == status] if status else []
        if owner is not None:
            where.append(Document.owner_id == owner)
        with self._sessions() as session:
            total = session.scalar(select(func.count()).select_from(Document).where(*where))
            items = session.scalars(
                select(Document)
                .where(*where)
                .order_by(Document.created_at.desc(), Document.id)
                .limit(limit)
                .offset(offset)
            ).all()
        return DocumentPage(list(items), total or 0)

    def delete(self, document_id: str, owner: str | None = None) -> bool:
        """Delete a document in any status, with its chunks and stored upload (cascade).

        Deleting one that is being processed is safe: the worker's completion finds the row
        gone and discards its work.
        """
        with self._sessions() as session:
            document = session.get(Document, document_id)
            if document is None or (owner is not None and document.owner_id != owner):
                return False
            session.delete(document)
            session.commit()
        return True

    def delete_failed(self, older_than: datetime, *, dry_run: bool = False) -> list[str]:
        """Delete failed documents last updated before ``older_than``, with their stored
        uploads (retention). Returns their ids, oldest first; deletes nothing on a dry run."""
        with self._sessions() as session:
            documents = session.scalars(
                select(Document)
                .where(Document.status == "failed", Document.updated_at < older_than)
                .order_by(Document.updated_at)
            ).all()
            ids = [document.id for document in documents]
            if not dry_run:
                for document in documents:
                    session.delete(document)
                session.commit()
        logger.info("retention.failed_documents", extra={"matched": len(ids), "dry_run": dry_run})
        return ids

    def status_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(get_args(DocumentStatus), 0)
        with self._sessions() as session:
            rows = session.execute(
                select(Document.status, func.count()).group_by(Document.status)
            ).all()
        counts.update(dict(rows))
        return counts

    def index_stats(self, embedding_model: str) -> dict[str, int]:
        """Document and chunk counts, and chunks embedded by a model other than this one."""
        with self._sessions() as session:
            documents = session.scalar(select(func.count()).select_from(Document)) or 0
            chunks = session.scalar(select(func.count()).select_from(Chunk)) or 0
            stale = (
                session.scalar(
                    select(func.count())
                    .select_from(Chunk)
                    .where(Chunk.embedding_model != embedding_model)
                )
                or 0
            )
        return {"documents": documents, "chunks": chunks, "stale_chunks": stale}

    def oldest_queued_at(self) -> datetime | None:
        """When the longest-waiting queued document was queued, or None if none waits."""
        with self._sessions() as session:
            oldest = session.scalar(
                select(func.min(Document.updated_at)).where(Document.status == "queued")
            )
        if oldest is None:
            return None
        # SQLite returns the stored UTC time without its zone.
        return oldest if oldest.tzinfo else oldest.replace(tzinfo=UTC)
