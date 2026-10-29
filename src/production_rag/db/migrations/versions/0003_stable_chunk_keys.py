"""Stable chunk keys: an identifier that survives re-ingestion of identical content.

`chunks.id` is a row id, so it changes when a document is deleted and ingested again.
`chunk_key` is derived from the document's content hash and the chunk's character span
(`production_rag.ingestion.chunking.chunk_key`), so identical bytes chunked with the same
settings always get the same keys. Existing chunks are backfilled with the same function.

The column is added in place with a constant default and then backfilled; a batch
(copy-and-rename) migration would cascade-delete through the foreign keys.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def chunk_key(content_hash: str, start: int, end: int) -> str:
    # A frozen copy of production_rag.ingestion.chunking.chunk_key: a migration must keep
    # doing what it did when it was written. A test checks the two still agree.
    return hashlib.sha256(f"{content_hash}:{start}:{end}".encode()).hexdigest()[:32]


def upgrade() -> None:
    op.add_column(
        "chunks", sa.Column("chunk_key", sa.String(32), nullable=False, server_default="")
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT chunks.id, documents.content_hash, chunks.start_char, chunks.end_char "
            "FROM chunks JOIN documents ON documents.id = chunks.document_id"
        )
    ).all()
    for chunk_id, content_hash, start, end in rows:
        bind.execute(
            sa.text("UPDATE chunks SET chunk_key = :key WHERE id = :id"),
            {"key": chunk_key(content_hash, start, end), "id": chunk_id},
        )
    op.create_index("ix_chunks_chunk_key", "chunks", ["chunk_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_chunks_chunk_key", table_name="chunks")
    op.drop_column("chunks", "chunk_key")
