"""Ingestion status (FR-3): document processing state, and uploads awaiting processing.

Documents that already exist were ingested synchronously, so they become ``ready`` with one
attempt. Columns are added in place: a batch (copy-and-rename) migration of ``documents``
would cascade-delete every chunk, because foreign keys are enforced.

Downgrading deletes documents that are not ready: revision 0001 has no way to represent
them, and they have no chunks.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_COLUMNS = (
    "status",
    "error_code",
    "error_message",
    "attempts",
    "claim_token",
    "lease_expires_at",
    "updated_at",
)


def upgrade() -> None:
    # SQLite only adds NOT NULL columns with a constant default; the ORM sets every value
    # for new rows, so the defaults only describe the documents that already exist.
    op.add_column(
        "documents", sa.Column("status", sa.String(16), nullable=False, server_default="ready")
    )
    op.add_column("documents", sa.Column("error_code", sa.String(64), nullable=True))
    op.add_column("documents", sa.Column("error_message", sa.Text, nullable=True))
    op.add_column(
        "documents", sa.Column("attempts", sa.Integer, nullable=False, server_default="1")
    )
    op.add_column("documents", sa.Column("claim_token", sa.String(36), nullable=True))
    op.add_column(
        "documents", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "documents",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default="1970-01-01 00:00:00.000000",
        ),
    )
    op.execute("UPDATE documents SET updated_at = created_at")
    op.create_index("ix_documents_status_updated_at", "documents", ["status", "updated_at"])

    op.create_table(
        "document_files",
        sa.Column(
            "document_id",
            sa.String(36),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("data", sa.LargeBinary, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("document_files")
    op.execute("DELETE FROM documents WHERE status != 'ready'")
    op.drop_index("ix_documents_status_updated_at", table_name="documents")
    for column in reversed(_NEW_COLUMNS):
        op.drop_column("documents", column)
