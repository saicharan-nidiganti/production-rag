"""Wires the application's components from settings. Shared by the API, the CLI and the
evaluation runner; it holds no behavior of its own beyond construction and shutdown."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from production_rag.accounts import AccountService
from production_rag.config import Settings
from production_rag.db import create_db_engine, session_factory, vector_index
from production_rag.db.documents import DocumentRepository
from production_rag.embeddings import Embedder, build_embedder
from production_rag.generation import Generator, build_generator
from production_rag.ingestion import IngestionService
from production_rag.rag import RagService
from production_rag.retrieval import Retriever, build_reranker


@dataclass
class Container:
    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    embedder: Embedder
    documents: DocumentRepository
    accounts: AccountService
    ingestion: IngestionService
    rag: RagService

    def index_stats(self) -> dict[str, int]:
        return self.documents.index_stats(self.embedder.model_id)

    def embedding_dimensions(self) -> int:
        """The embedder's vector length, from one probe embedding."""
        return int(self.embedder.embed_query("dimensions").shape[0])

    def build_vector_index(self) -> dict[str, Any] | None:
        """With approximate search, build the model's HNSW index; None otherwise.
        Evaluation and the benchmark call this once their scratch database is filled, so
        they measure the search as configured. A deployment runs `rag db
        create-vector-index` instead."""
        if self.settings.vector_search != "approximate":
            return None
        return vector_index.create(
            self.settings.database_url, self.embedder.model_id, self.embedding_dimensions()
        )

    def close(self) -> None:
        self.engine.dispose()


def build_container(settings: Settings, *, generator: Generator | None = None) -> Container:
    engine = create_db_engine(settings.database_url)
    sessions = session_factory(engine)
    embedder = build_embedder(settings)
    retriever = Retriever(
        sessions,
        embedder,
        candidate_pool=settings.candidate_pool,
        rrf_k=settings.rrf_k,
        reranker=build_reranker(settings),
        rerank_candidates=settings.rerank_candidates,
        approximate_vector_search=settings.vector_search == "approximate",
        hnsw_ef_search=settings.hnsw_ef_search,
    )
    return Container(
        settings=settings,
        engine=engine,
        sessions=sessions,
        embedder=embedder,
        documents=DocumentRepository(sessions),
        accounts=AccountService(sessions, settings),
        ingestion=IngestionService(sessions, embedder, settings),
        rag=RagService(retriever, generator or build_generator(settings), settings),
    )
