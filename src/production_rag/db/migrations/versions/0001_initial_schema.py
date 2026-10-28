"""Initial schema: documents, chunks, FTS5 index and index generation counter.

Revision ID: 0001
Revises:
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUMP_GENERATION = "UPDATE index_state SET generation = generation + 1 WHERE id = 1;"


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_name", sa.String(512), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False),
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
        sa.Column("embedding", sa.LargeBinary, nullable=False),
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
    for trigger in ("chunks_au", "chunks_ad", "chunks_ai"):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute("DROP TABLE IF EXISTS chunks_fts")
    op.drop_table("index_state")
    op.drop_index("ix_chunks_embedding_model", table_name="chunks")
    op.drop_index("ix_chunks_document_id", table_name="chunks")
    op.drop_table("chunks")
    op.drop_index("ix_documents_content_hash", table_name="documents")
    op.drop_table("documents")
