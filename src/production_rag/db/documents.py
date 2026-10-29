"""Queries over stored documents, for the API, the CLI and ingestion.

Writes that change a document's processing state go through ``ingestion.queue`` instead,
because they must respect the claim protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import get_args

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from production_rag.db.models import Chunk, Document, DocumentStatus


@dataclass(frozen=True)
class DocumentPage:
    items: list[Document]
    total: int  # all documents matching the filter, not just this page


class DocumentRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def get(self, document_id: str) -> Document | None:
        with self._sessions() as session:
            return session.get(Document, document_id)

    def list_documents(
        self, *, limit: int, offset: int, status: DocumentStatus | None = None
    ) -> DocumentPage:
        """Newest first."""
        where = [Document.status == status] if status else []
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

    def delete(self, document_id: str) -> bool:
        """Delete a document in any status, with its chunks and stored upload (cascade).

        Deleting one that is being processed is safe: the worker's completion finds the row
        gone and discards its work.
        """
        with self._sessions() as session:
            document = session.get(Document, document_id)
            if document is None:
                return False
            session.delete(document)
            session.commit()
        return True

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
