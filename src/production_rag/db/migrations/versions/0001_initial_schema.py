"""Initial schema: documents, chunks, FTS5 index and index generation counter.

Revision ID: 0001
Revises:
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from production_rag.db.types import PgVector

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUMP_GENERATION = "UPDATE index_state SET generation = generation + 1 WHERE id = 1;"


def _postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    postgres = _postgres()
    embedding: sa.types.TypeEngine[Any] = PgVector() if postgres else sa.LargeBinary()
    if postgres:
        # Needs a role allowed to create extensions, once per database.
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_name", sa.String(512), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        # SQLite ignores the length; PostgreSQL enforces it, and media types run past 64.
        sa.Column("content_type", sa.String(255 if postgres else 64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("num_chunks", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("metadata", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_documents_content_hash", "documents", ["content_hash"], unique=True)

    op.create_table(
        "chunks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "document_id",
            sa.String(36),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("start_char", sa.Integer, nullable=False),
        sa.Column("end_char", sa.Integer, nullable=False),
        sa.Column("heading", sa.String(512), nullable=True),
        sa.Column("page", sa.Integer, nullable=True),
        sa.Column("embedding", embedding, nullable=False),
        sa.Column("embedding_model", sa.String(128), nullable=False),
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.create_index("ix_chunks_embedding_model", "chunks", ["embedding_model"])

    op.create_table(
        "index_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("generation", sa.Integer, nullable=False),
    )
    op.execute("INSERT INTO index_state (id, generation) VALUES (1, 0)")

    if postgres:
        # The same search, the PostgreSQL way: a stored tsvector the database maintains,
        # with a GIN index. No triggers: vectors are searched in the database too, so
        # there is no in-process index to tell about changes.
        op.execute(
            "ALTER TABLE chunks ADD COLUMN text_search tsvector "
            "GENERATED ALWAYS AS (to_tsvector('english', text)) STORED"
        )
        op.execute("CREATE INDEX ix_chunks_text_search ON chunks USING gin (text_search)")
        return

    # External-content FTS5 index over chunks.text, kept in sync by triggers.
    op.execute(
        "CREATE VIRTUAL TABLE chunks_fts USING fts5("
        "text, content='chunks', content_rowid='id', tokenize='porter unicode61')"
    )
    op.execute(
        f"""
        CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
            {_BUMP_GENERATION}
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
            {_BUMP_GENERATION}
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
            INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
            {_BUMP_GENERATION}
        END
        """
    )


def downgrade() -> None:
    if not _postgres():
        for trigger in ("chunks_au", "chunks_ad", "chunks_ai"):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        op.execute("DROP TABLE IF EXISTS chunks_fts")
    op.drop_table("index_state")
    op.drop_index("ix_chunks_embedding_model", table_name="chunks")
    op.drop_index("ix_chunks_document_id", table_name="chunks")
    op.drop_table("chunks")
    op.drop_index("ix_documents_content_hash", table_name="documents")
    op.drop_table("documents")
