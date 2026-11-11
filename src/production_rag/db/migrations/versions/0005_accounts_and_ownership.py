"""Accounts, and an owner for every document (ADR-0012).

New tables: ``users``, ``auth_sessions`` (server-side sessions; the row id is the SHA-256
of the session token, so a stolen database holds no usable token) and ``api_tokens``
(personal tokens, stored the same way).

``documents.owner_id`` names the user a document belongs to. Existing documents, and all
documents while accounts are off, belong to ``''``: one shared space, as before.

Two uniqueness rules become per owner, because two users may upload the same file:

- a document's content hash is unique per owner, not across the table;
- a chunk key is unique within its document. The key is derived from the content, so
  the same content owned by two users yields the same keys.

Both were plain indexes, so they are replaced in place: a table rebuild would
cascade-delete through the foreign keys.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_tokens_token_hash", "api_tokens", ["token_hash"], unique=True)
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])

    op.add_column(
        "documents", sa.Column("owner_id", sa.String(36), nullable=False, server_default="")
    )
    op.drop_index("ix_documents_content_hash", table_name="documents")
    op.create_index(
        "ix_documents_owner_content_hash", "documents", ["owner_id", "content_hash"], unique=True
    )
    op.drop_index("ix_chunks_chunk_key", table_name="chunks")
    op.create_index("ix_chunks_chunk_key", "chunks", ["chunk_key"])
    op.create_index(
        "ix_chunks_document_chunk_key", "chunks", ["document_id", "chunk_key"], unique=True
    )


def downgrade() -> None:
    # Revision 0004 has one shared space: only its documents can be represented there.
    op.execute("DELETE FROM documents WHERE owner_id != ''")
    op.drop_index("ix_chunks_document_chunk_key", table_name="chunks")
    op.drop_index("ix_chunks_chunk_key", table_name="chunks")
    op.create_index("ix_chunks_chunk_key", "chunks", ["chunk_key"], unique=True)
    op.drop_index("ix_documents_owner_content_hash", table_name="documents")
    op.create_index("ix_documents_content_hash", "documents", ["content_hash"], unique=True)
    op.drop_column("documents", "owner_id")
    op.drop_table("api_tokens")
    op.drop_table("auth_sessions")
    op.drop_table("users")
