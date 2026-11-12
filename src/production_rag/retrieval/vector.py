"""Cosine search over chunk embeddings.

On SQLite the search is exact, over a matrix held in memory. The matrix is reloaded only
when ``index_state.generation`` changes. Triggers bump the counter on every chunk write, so
the cache stays correct even when another process ingests documents. Exact search is
O(n * d) per query; measured on a laptop CPU it takes ~0.5 ms for 10k chunks and ~6 ms for
100k chunks (d=384, 154 MB of vectors).

On PostgreSQL pgvector runs the search: exactly by default, or, with
``RAG_VECTOR_SEARCH=approximate``, through the model's HNSW index (``db/vector_index.py``,
ADR-0014).
"""

from __future__ import annotations

import threading

import numpy as np
import numpy.typing as npt
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from production_rag import metrics
from production_rag.db.engine import is_postgres
from production_rag.db.models import Chunk, Document, IndexState
from production_rag.db.types import vector_literal
from production_rag.embeddings import Vector, from_blob

_POSTGRES_SEARCH = (
    "SELECT chunks.id AS id, -(chunks.embedding <#> CAST(:query AS vector)) AS score "
    "FROM chunks JOIN documents ON documents.id = chunks.document_id "
    "WHERE chunks.embedding_model = :model {owner}"
    "ORDER BY chunks.embedding <#> CAST(:query AS vector), chunks.id LIMIT :k"
)
_POSTGRES_ANY_OWNER = _POSTGRES_SEARCH.format(owner="")
_POSTGRES_ONE_OWNER = _POSTGRES_SEARCH.format(owner="AND documents.owner_id = :owner ")

# The index is on the expression embedding::vector(d) for one model's rows, so the search
# orders by exactly that expression under the same model condition, or the planner can't
# use it. Iterative scans keep the index scan going until LIMIT rows pass the owner filter;
# relaxed order lets them arrive slightly out of order, so the materialized CTE keeps the
# LIMIT on the scan and the outer query sorts. "+ 0" stops PostgreSQL 17 and later from
# skipping that sort (pgvector's README).
_APPROXIMATE_SEARCH = (
    "WITH nearest AS MATERIALIZED ("
    "SELECT chunks.id AS id, "
    "(chunks.embedding::vector({d})) <#> CAST(:query AS vector({d})) AS distance "
    "FROM chunks JOIN documents ON documents.id = chunks.document_id "
    "WHERE chunks.embedding_model = :model {owner}"
    "ORDER BY (chunks.embedding::vector({d})) <#> CAST(:query AS vector({d})) LIMIT :k"
    ") SELECT id, -distance AS score FROM nearest ORDER BY distance + 0, id"
)
_SCAN_SETTINGS = text(
    "SELECT set_config('hnsw.ef_search', :ef_search, true), "
    "set_config('hnsw.iterative_scan', 'relaxed_order', true)"
)


class VectorIndex:
    def __init__(self, model_id: str, *, approximate: bool = False, ef_search: int = 200) -> None:
        self.model_id = model_id
        self.approximate = approximate  # PostgreSQL only
        self.ef_search = ef_search
        self._lock = threading.Lock()
        self._generation: int | None = None
        self._ids: npt.NDArray[np.int64] = np.zeros(0, dtype=np.int64)
        self._matrix: Vector = np.zeros((0, 0), dtype=np.float32)
        # The matrix is ordered by owner, then chunk id, so each owner's rows are one range
        # and a search slices them out: a view, not a copy. Selecting them by index array
        # copied the owner's rows on every search; with accounts off, the whole matrix,
        # which made each search twelve times slower at 10,000 chunks.
        self._range_of: dict[str, tuple[int, int]] = {}

    def _refresh(
        self, session: Session
    ) -> tuple[npt.NDArray[np.int64], Vector, dict[str, tuple[int, int]]]:
        generation = session.scalar(select(IndexState.generation).where(IndexState.id == 1))
        with self._lock:
            if generation != self._generation:
                rows = session.execute(
                    select(Chunk.id, Chunk.embedding, Document.owner_id)
                    .join(Document, Document.id == Chunk.document_id)
                    .where(Chunk.embedding_model == self.model_id)
                    .order_by(Document.owner_id, Chunk.id)
                ).all()
                self._ids = np.array([r.id for r in rows], dtype=np.int64)
                self._matrix = (
                    np.stack([from_blob(r.embedding) for r in rows])
                    if rows
                    else np.zeros((0, 0), dtype=np.float32)
                )
                ranges: dict[str, tuple[int, int]] = {}
                for position, row in enumerate(rows):
                    start = ranges.get(row.owner_id, (position, position))[0]
                    ranges[row.owner_id] = (start, position + 1)
                self._range_of = ranges
                self._generation = generation
            return self._ids, self._matrix, self._range_of

    def search(
        self,
        session: Session,
        query: Vector,
        k: int,
        owner: str | None = None,
        *,
        exact: bool = False,
    ) -> list[tuple[int, float]]:
        """The ``k`` nearest chunks. With ``owner``, only that owner's chunks are scored at
        all: another owner's can't be returned, ranked against, or leak through a score.
        ``exact`` forces an exact search where the index would otherwise be used."""
        if is_postgres(session):
            if self.approximate and not exact:
                hits = self._approximate_search(session, query, k, owner)
                if len(hits) >= k:
                    return hits
                # Fewer than k: the owner, or the whole library, has fewer chunks than k;
                # or the scan reached hnsw.max_scan_tuples before finding k of this owner's.
                # The exact search settles which, and for a small owner it is cheap.
                metrics.VECTOR_EXACT_FALLBACKS.inc()
            return self._postgres_search(session, query, k, owner)
        ids, matrix, range_of = self._refresh(session)
        if owner is not None:
            span = range_of.get(owner)
            if span is None:
                return []
            ids, matrix = ids[span[0] : span[1]], matrix[span[0] : span[1]]
        if len(ids) == 0:
            return []
        scores = matrix @ query
        k = min(k, len(ids))
        top = np.argpartition(-scores, k - 1)[:k]
        # Highest score first; equal scores by chunk id, whatever the rows' order.
        top = top[np.lexsort((ids[top], -scores[top]))]
        return [(int(ids[i]), float(scores[i])) for i in top]

    def _postgres_search(
        self, session: Session, query: Vector, k: int, owner: str | None
    ) -> list[tuple[int, float]]:
        """The same exact search, run by pgvector. Embeddings are unit vectors, so the inner
        product is the cosine; ``<#>`` is its negative, which sorts nearest first. Nothing
        is cached in this process, so every instance sees the same index at once."""
        parameters: dict[str, object] = {
            "query": vector_literal(query),
            "model": self.model_id,
            "k": k,
        }
        if owner is None:
            statement = _POSTGRES_ANY_OWNER
        else:
            statement = _POSTGRES_ONE_OWNER
            parameters["owner"] = owner
        rows = session.execute(text(statement), parameters).all()
        return [(int(row.id), float(row.score)) for row in rows]

    def approximate_plan(
        self, session: Session, query: Vector, k: int, owner: str | None = None
    ) -> list[str]:
        """PostgreSQL's plan for the approximate search, so a check can confirm the index
        is used: the planner may rightly prefer an exact scan of a small library."""
        statement, parameters = self._approximate_statement(session, query, k, owner)
        rows = session.execute(text("EXPLAIN " + statement), parameters).all()
        return [str(row[0]) for row in rows]

    def _approximate_search(
        self, session: Session, query: Vector, k: int, owner: str | None
    ) -> list[tuple[int, float]]:
        """The search through the model's HNSW index."""
        statement, parameters = self._approximate_statement(session, query, k, owner)
        rows = session.execute(text(statement), parameters).all()
        return [(int(row.id), float(row.score)) for row in rows]

    def _approximate_statement(
        self, session: Session, query: Vector, k: int, owner: str | None
    ) -> tuple[str, dict[str, object]]:
        # The scan settings last until the end of the current transaction, so they affect
        # only this request's searches.
        session.execute(_SCAN_SETTINGS, {"ef_search": str(self.ef_search)})
        parameters: dict[str, object] = {
            "query": vector_literal(query),
            "model": self.model_id,
            "k": k,
        }
        owner_condition = ""
        if owner is not None:
            owner_condition = "AND documents.owner_id = :owner "
            parameters["owner"] = owner
        # The dimension is an integer from the query vector itself, not outside input.
        statement = _APPROXIMATE_SEARCH.format(d=int(query.shape[0]), owner=owner_condition)
        return statement, parameters
