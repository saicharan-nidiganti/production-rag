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

# An owner's search on SQLite. FTS5 ranks "ORDER BY rank LIMIT" straight from its index only
# when the query reads chunks_fts alone; joined to documents for the owner, SQLite ranks
# every match and joins each one (30 ms against 7 at 10,000 chunks). So the statement takes
# the best m matches from the index, then keeps the owner's, filtered in the same statement.
# Those are the owner's best k whenever k of them are among the m: any better match of
# theirs would be there too. If not, and the m didn't exhaust the matches, m grows: a small
# library is never crowded out by a large one. "scanned" comes back even with no match.
_SQLITE_ONE_OWNER = (
    "WITH hits AS MATERIALIZED ("
    "SELECT rowid, bm25(chunks_fts) AS rank FROM chunks_fts "
    "WHERE chunks_fts MATCH :query ORDER BY rank LIMIT :m), "
    "mine AS (SELECT hits.rowid AS rowid, hits.rank AS rank FROM hits "
    "JOIN chunks ON chunks.id = hits.rowid "
    "JOIN documents ON documents.id = chunks.document_id "
    "WHERE documents.owner_id = :owner ORDER BY hits.rank LIMIT :k) "
    "SELECT (SELECT count(*) FROM hits) AS scanned, mine.rowid AS rowid, mine.rank AS rank "
    "FROM (SELECT 1) LEFT JOIN mine ORDER BY mine.rank"
)


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
    """The ``k`` best BM25 matches. With ``owner``, the filter is part of the statement, and
    the limit applies to that owner's chunks, not to everyone's."""
    if is_postgres(session):
        return _postgres_search(session, query, k, owner)
    fts_query = build_fts_query(query)
    if fts_query is None:
        return []
    # FTS5's bm25() is negative, and more negative means more relevant.
    if owner is None:
        rows = session.execute(
            text(
                "SELECT rowid, bm25(chunks_fts) AS rank FROM chunks_fts "
                "WHERE chunks_fts MATCH :query ORDER BY rank LIMIT :k"
            ),
            {"query": fts_query, "k": k},
        ).all()
        return [(int(row.rowid), -float(row.rank)) for row in rows]
    scan = max(4 * k, 64)
    while True:
        rows = session.execute(
            text(_SQLITE_ONE_OWNER), {"query": fts_query, "m": scan, "k": k, "owner": owner}
        ).all()
        hits = [(int(row.rowid), -float(row.rank)) for row in rows if row.rowid is not None]
        if len(hits) >= k or int(rows[0].scanned) < scan:
            return hits
        scan *= 4


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
