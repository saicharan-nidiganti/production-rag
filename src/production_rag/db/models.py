"""ORM models.

The FTS5 index (``chunks_fts``) and the triggers that keep it in sync with ``chunks`` are
created by the migration in raw SQL; they are not modelled here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# queued -> processing -> ready | failed. Only ready documents have chunks.
DocumentStatus = Literal["queued", "processing", "ready", "failed"]
Role = Literal["admin", "user"]
# The owner of every document while accounts are off: one shared space.
SHARED_OWNER = ""


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_status_updated_at", "status", "updated_at"),
        # The same bytes are one document per owner; two owners may each have them.
        Index("ix_documents_owner_content_hash", "owner_id", "content_hash", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_name: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    # The user the document belongs to; SHARED_OWNER while accounts are off. Every query
    # that reads documents or chunks filters on it (NFR-1).
    owner_id: Mapped[str] = mapped_column(String(36), default=SHARED_OWNER, server_default="")
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
    # The document this one is a new version of; deleted when this one becomes ready. Not a
    # foreign key: the id is kept as lineage after that document is gone.
    replaces_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

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
    # Unique within a document: the key comes from the content, which two owners may share.
    __table_args__ = (
        Index("ix_chunks_document_chunk_key", "document_id", "chunk_key", unique=True),
    )

    # INTEGER PRIMARY KEY aliases SQLite's rowid, which keeps it stable across VACUUM;
    # the external-content FTS5 index relies on that.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    # Stable across re-ingestion of identical content (ingestion/chunking.py: chunk_key);
    # `id` is a database row id and changes when a document is deleted and re-ingested.
    chunk_key: Mapped[str] = mapped_column(String(32), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    start_char: Mapped[int] = mapped_column(Integer)
    end_char: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(String(512), nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)
    embedding_model: Mapped[str] = mapped_column(String(128), index=True)

    document: Mapped[Document] = relationship(back_populates="chunks")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Stored lower-cased; the address is the sign-in name.
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))  # Argon2id, with its parameters
    role: Mapped[str] = mapped_column(String(16), default="user", server_default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthSession(Base):
    """A signed-in browser. ``id`` is the SHA-256 of the cookie's token, never the token."""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ApiToken(Base):
    """A personal token for scripts. Only its SHA-256 is stored; it is shown once."""

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IndexState(Base):
    """Single-row table whose generation counter changes whenever chunks change.

    In-process vector caches compare against it to know when to reload, which keeps
    multiple worker processes consistent without shared memory.
    """

    __tablename__ = "index_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
