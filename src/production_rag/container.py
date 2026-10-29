"""Wires the application's components from settings. Shared by the API, the CLI and the
evaluation runner; it holds no behavior of its own beyond construction and shutdown."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from production_rag.config import Settings
from production_rag.db import create_db_engine, session_factory
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
    ingestion: IngestionService
    rag: RagService

    def index_stats(self) -> dict[str, int]:
        return self.documents.index_stats(self.embedder.model_id)

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
    )
    return Container(
        settings=settings,
        engine=engine,
        sessions=sessions,
        embedder=embedder,
        documents=DocumentRepository(sessions),
        ingestion=IngestionService(sessions, embedder, settings),
        rag=RagService(retriever, generator or build_generator(settings), settings),
    )
