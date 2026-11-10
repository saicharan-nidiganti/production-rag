"""Document replacement: an upload can name the document it is a new version of.

`documents.replaces_id` holds the id of the document an upload replaces. The replaced
document is deleted in the transaction that marks its replacement ready, so searches never
see both versions, or neither. The id is kept afterwards as lineage, which is why it isn't
a foreign key: the document it names no longer exists.

Added in place: a batch (copy-and-rename) migration would cascade-delete through the
foreign keys.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("replaces_id", sa.String(36), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "replaces_id")
