"""Retrieval facade: vector, lexical or hybrid search, optional cross-encoder reranking,
returning hydrated chunks."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from production_rag.config import RetrievalMode
from production_rag.db.models import Chunk, Document
from production_rag.embeddings import Embedder
from production_rag.retrieval.fusion import reciprocal_rank_fusion
from production_rag.retrieval.lexical import lexical_search
from production_rag.retrieval.reranking import Reranker
from production_rag.retrieval.vector import VectorIndex


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int  # database row id; changes if the document is deleted and re-ingested
    chunk_key: str  # stable for identical content and chunk settings
    document_id: str
    document_title: str
    source_name: str
    heading: str | None
    page: int | None
    text: str
    start_char: int
    end_char: int
    score: float  # the final ranking score: rerank score if reranked, else cosine, BM25 or RRF
    vector_score: float | None
    lexical_score: float | None
    rerank_score: float | None = None


@dataclass(frozen=True)
class SearchResult:
    chunks: list[RetrievedChunk]
    mode: RetrievalMode
    best_vector_score: float | None  # highest cosine over the whole index; drives abstention
    best_rerank_score: float | None = None  # highest cross-encoder score, when reranking ran
    rerank_ms: float | None = None


class Retriever:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        embedder: Embedder,
        *,
        candidate_pool: int = 30,
        rrf_k: int = 60,
        reranker: Reranker | None = None,
        rerank_candidates: int = 5,
    ) -> None:
        self._sessions = sessions
        self._embedder = embedder
        self._index = VectorIndex(embedder.model_id)
        self._candidate_pool = candidate_pool
        self._rrf_k = rrf_k
        self._reranker = reranker
        self._rerank_candidates = rerank_candidates

    def search(self, query: str, *, top_k: int, mode: RetrievalMode) -> SearchResult:
        # With a reranker, more fused candidates are hydrated so it can promote one from
        # below the top_k cut.
        depth = max(top_k, self._rerank_candidates) if self._reranker else top_k
        pool = max(depth, self._candidate_pool)
        with self._sessions() as session:
            vector_hits: list[tuple[int, float]] = []
            lexical_hits: list[tuple[int, float]] = []
            if mode in ("vector", "hybrid"):
                query_vector = self._embedder.embed_query(query)
                vector_hits = self._index.search(session, query_vector, pool)
            if mode in ("lexical", "hybrid"):
                lexical_hits = lexical_search(session, query, pool)

            if mode == "vector":
                ranked = vector_hits[:depth]
            elif mode == "lexical":
                ranked = lexical_hits[:depth]
            else:
                ranked = reciprocal_rank_fusion(
                    [[i for i, _ in vector_hits], [i for i, _ in lexical_hits]], k=self._rrf_k
                )[:depth]

            chunks = self._hydrate(session, ranked, dict(vector_hits), dict(lexical_hits))

        best_vector = vector_hits[0][1] if vector_hits else None
        if self._reranker is None or not chunks:
            return SearchResult(chunks=chunks[:top_k], mode=mode, best_vector_score=best_vector)

        start = time.perf_counter()
        scores = self._reranker.score(query, [chunk.text for chunk in chunks])
        rerank_ms = round((time.perf_counter() - start) * 1000, 2)
        # Stable sort: equal rerank scores keep their fused order.
        order = sorted(range(len(chunks)), key=lambda i: -scores[i])
        reranked = [replace(chunks[i], score=scores[i], rerank_score=scores[i]) for i in order]
        return SearchResult(
            chunks=reranked[:top_k],
            mode=mode,
            best_vector_score=best_vector,
            best_rerank_score=max(scores),
            rerank_ms=rerank_ms,
        )

    @staticmethod
    def _hydrate(
        session: Session,
        ranked: list[tuple[int, float]],
        vector_scores: dict[int, float],
        lexical_scores: dict[int, float],
    ) -> list[RetrievedChunk]:
        if not ranked:
            return []
        ids = [chunk_id for chunk_id, _ in ranked]
        rows = session.execute(
            select(Chunk, Document.title, Document.source_name)
            .join(Document, Chunk.document_id == Document.id)
            .where(Chunk.id.in_(ids))
        ).all()
        by_id = {chunk.id: (chunk, title, source) for chunk, title, source in rows}
        results: list[RetrievedChunk] = []
        for chunk_id, score in ranked:
            if chunk_id not in by_id:  # deleted between ranking and hydration
                continue
            chunk, title, source = by_id[chunk_id]
            results.append(
                RetrievedChunk(
                    chunk_id=chunk.id,
                    chunk_key=chunk.chunk_key,
                    document_id=chunk.document_id,
                    document_title=title,
                    source_name=source,
                    heading=chunk.heading,
                    page=chunk.page,
                    text=chunk.text,
                    start_char=chunk.start_char,
                    end_char=chunk.end_char,
                    score=score,
                    vector_score=vector_scores.get(chunk_id),
                    lexical_score=lexical_scores.get(chunk_id),
                )
            )
        return results
