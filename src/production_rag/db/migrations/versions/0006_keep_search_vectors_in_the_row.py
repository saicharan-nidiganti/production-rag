"""Keep each chunk's search vector in its row, on PostgreSQL.

A chunk row holds its text, its embedding and `text_search`, more than the roughly 2 KB
PostgreSQL keeps in a row, so it moves the largest values to the table's TOAST storage:
the embedding, and then `text_search`, which is larger than the text. Keyword search reads
`text_search` for every matching chunk, so each match cost a TOAST lookup. `STORAGE MAIN`
makes PostgreSQL move the text out first: it is read only for the few chunks a search
returns. Results are unchanged; at 100,000 chunks keyword search took 51 ms instead of
172 at p50 (ADR-0015).

The setting applies to rows written from now on. `rag db rebuild-search-index` rewrites the
existing ones. SQLite has no TOAST, and no `text_search` column: nothing changes there.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE chunks ALTER COLUMN text_search SET STORAGE MAIN")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE chunks ALTER COLUMN text_search SET STORAGE EXTENDED")
