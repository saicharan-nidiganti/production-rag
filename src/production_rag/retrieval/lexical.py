"""BM25 keyword search over the SQLite FTS5 index."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from production_rag.text import content_tokens

_MAX_QUERY_TERMS = 64


def build_fts_query(query: str) -> str | None:
    """Turn free text into a safe FTS5 expression: quoted terms joined with OR.

    Quoting every term neutralises FTS5 operators (AND, NEAR, *, column filters) in user
    input. OR semantics plus BM25 ranking favours chunks that match more, and rarer, terms.
    """
    terms = list(dict.fromkeys(content_tokens(query)))[:_MAX_QUERY_TERMS]
    if not terms:
        return None
    return " OR ".join(f'"{term}"' for term in terms)


def lexical_search(
    session: Session, query: str, k: int, owner: str | None = None
) -> list[tuple[int, float]]:
    """The ``k`` best BM25 matches. With ``owner``, the filter is part of the query, so the
    limit applies to that owner's chunks, not to everyone's."""
    fts_query = build_fts_query(query)
    if fts_query is None:
        return []
    if owner is None:
        statement = (
            "SELECT rowid, bm25(chunks_fts) AS rank FROM chunks_fts "
            "WHERE chunks_fts MATCH :query ORDER BY rank LIMIT :k"
        )
        parameters: dict[str, object] = {"query": fts_query, "k": k}
    else:
        statement = (
            "SELECT chunks_fts.rowid AS rowid, bm25(chunks_fts) AS rank FROM chunks_fts "
            "JOIN chunks ON chunks.id = chunks_fts.rowid "
            "JOIN documents ON documents.id = chunks.document_id "
            "WHERE chunks_fts MATCH :query AND documents.owner_id = :owner "
            "ORDER BY rank LIMIT :k"
        )
        parameters = {"query": fts_query, "k": k, "owner": owner}
    rows = session.execute(text(statement), parameters).all()
    # FTS5's bm25() is negative, and more negative means more relevant.
    return [(int(row.rowid), -float(row.rank)) for row in rows]
