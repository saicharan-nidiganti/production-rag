"""Keyword search: BM25 over the SQLite FTS5 index, or ``ts_rank`` over a PostgreSQL
``tsvector``. Both stem English words and match any of the question's terms; only the
ranking function differs, and rank fusion uses the order, not the scores. ``ts_rank`` was
chosen over ``ts_rank_cd``: the same retrieval quality on golden v2, about 30% faster."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from production_rag.db.engine import is_postgres
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
    if is_postgres(session):
        return _postgres_search(session, query, k, owner)
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


_POSTGRES_SEARCH = (
    "SELECT chunks.id AS id, ts_rank(chunks.text_search, q) AS rank "
    "FROM chunks JOIN documents ON documents.id = chunks.document_id, "
    "to_tsquery('english', :query) AS q "
    "WHERE chunks.text_search @@ q {owner}"
    "ORDER BY rank DESC, chunks.id LIMIT :k"
)
_POSTGRES_ANY_OWNER = _POSTGRES_SEARCH.format(owner="")
_POSTGRES_ONE_OWNER = _POSTGRES_SEARCH.format(owner="AND documents.owner_id = :owner ")


def build_tsquery(query: str) -> str | None:
    """The same terms as ``build_fts_query``, as a PostgreSQL tsquery: quoted, joined with OR.

    The tokens are letters and digits only, and each is quoted, so nothing in the user's
    text can act as a tsquery operator.
    """
    terms = list(dict.fromkeys(content_tokens(query)))[:_MAX_QUERY_TERMS]
    if not terms:
        return None
    return " | ".join("'" + term.replace("'", "") + "'" for term in terms)


def _postgres_search(
    session: Session, query: str, k: int, owner: str | None
) -> list[tuple[int, float]]:
    tsquery = build_tsquery(query)
    if tsquery is None:
        return []
    parameters: dict[str, object] = {"query": tsquery, "k": k}
    if owner is None:
        statement = _POSTGRES_ANY_OWNER
    else:
        statement = _POSTGRES_ONE_OWNER
        parameters["owner"] = owner
    rows = session.execute(text(statement), parameters).all()
    return [(int(row.id), float(row.rank)) for row in rows]
