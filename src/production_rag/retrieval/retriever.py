"""Retrieval facade: vector, lexical or hybrid search returning hydrated chunks."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from production_rag.config import RetrievalMode
from production_rag.db.models import Chunk, Document
from production_rag.embeddings import Embedder
from production_rag.retrieval.fusion import reciprocal_rank_fusion
from production_rag.retrieval.lexical import lexical_search
from production_rag.retrieval.vector import VectorIndex


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    document_id: str
    document_title: str
    source_name: str
    heading: str | None
    page: int | None
    text: str
    start_char: int
    end_char: int
    score: float  # the ranking score for the chosen mode (cosine, BM25 or RRF)
    vector_score: float | None
    lexical_score: float | None


@dataclass(frozen=True)
class SearchResult:
    chunks: list[RetrievedChunk]
    mode: RetrievalMode
    best_vector_score: float | None  # highest cosine over the whole index; drives abstention


class Retriever:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        embedder: Embedder,
        *,
        candidate_pool: int = 30,
        rrf_k: int = 60,
    ) -> None:
        self._sessions = sessions
        self._embedder = embedder
        self._index = VectorIndex(embedder.model_id)
        self._candidate_pool = candidate_pool
        self._rrf_k = rrf_k

    def search(self, query: str, *, top_k: int, mode: RetrievalMode) -> SearchResult:
        pool = max(top_k, self._candidate_pool)
        with self._sessions() as session:
            vector_hits: list[tuple[int, float]] = []
            lexical_hits: list[tuple[int, float]] = []
            if mode in ("vector", "hybrid"):
                query_vector = self._embedder.embed_query(query)
                vector_hits = self._index.search(session, query_vector, pool)
            if mode in ("lexical", "hybrid"):
                lexical_hits = lexical_search(session, query, pool)

            if mode == "vector":
                ranked = vector_hits[:top_k]
            elif mode == "lexical":
                ranked = lexical_hits[:top_k]
            else:
                ranked = reciprocal_rank_fusion(
                    [[i for i, _ in vector_hits], [i for i, _ in lexical_hits]], k=self._rrf_k
                )[:top_k]

            chunks = self._hydrate(session, ranked, dict(vector_hits), dict(lexical_hits))

        best_vector = vector_hits[0][1] if vector_hits else None
        return SearchResult(chunks=chunks, mode=mode, best_vector_score=best_vector)

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
