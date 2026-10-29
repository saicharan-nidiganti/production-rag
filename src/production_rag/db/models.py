"""ORM models.

The FTS5 index (``chunks_fts``) and the triggers that keep it in sync with ``chunks`` are
created by the migration in raw SQL; they are not modelled here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# queued -> processing -> ready | failed. Only ready documents have chunks.
DocumentStatus = Literal["queued", "processing", "ready", "failed"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (Index("ix_documents_status_updated_at", "status", "updated_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_name: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    num_chunks: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Processing state (FR-3). Title, text and num_chunks are placeholders until "ready".
    status: Mapped[str] = mapped_column(String(16), default="queued")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Set while processing: the claiming worker's token, and when its claim lapses unless
    # renewed. A lapsed claim means the worker stopped; another one may take the document.
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Chunk.ordinal",
    )


class DocumentFile(Base):
    """Uploaded bytes awaiting processing.

    Kept while the document is queued, processing or failed, so processing survives a
    restart and a failed document can be retried; deleted when the document becomes ready.
    """

    __tablename__ = "document_files"

    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    data: Mapped[bytes] = mapped_column(LargeBinary)


class Chunk(Base):
    __tablename__ = "chunks"

    # INTEGER PRIMARY KEY aliases SQLite's rowid, which keeps it stable across VACUUM;
    # the external-content FTS5 index relies on that.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    start_char: Mapped[int] = mapped_column(Integer)
    end_char: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(String(512), nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)
    embedding_model: Mapped[str] = mapped_column(String(128), index=True)

    document: Mapped[Document] = relationship(back_populates="chunks")


class IndexState(Base):
    """Single-row table whose generation counter changes whenever chunks change.

    In-process vector caches compare against it to know when to reload, which keeps
    multiple worker processes consistent without shared memory.
    """

    __tablename__ = "index_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
