"""Wires the application's components from settings. Shared by the API and the CLI."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from production_rag.config import Settings
from production_rag.db import create_db_engine, session_factory
from production_rag.db.models import Chunk, Document
from production_rag.embeddings import Embedder, build_embedder, to_blob
from production_rag.generation import Generator, build_generator
from production_rag.ingestion import IngestionService, embedding_input
from production_rag.rag import RagService
from production_rag.retrieval import Retriever


@dataclass
class Container:
    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    embedder: Embedder
    ingestion: IngestionService
    rag: RagService

    def index_stats(self) -> dict[str, int]:
        with self.sessions() as session:
            documents = session.scalar(select(func.count()).select_from(Document)) or 0
            chunks = session.scalar(select(func.count()).select_from(Chunk)) or 0
            stale = (
                session.scalar(
                    select(func.count())
                    .select_from(Chunk)
                    .where(Chunk.embedding_model != self.embedder.model_id)
                )
                or 0
            )
        return {"documents": documents, "chunks": chunks, "stale_chunks": stale}

    def reindex(self, batch_size: int = 256) -> int:
        """Re-embed chunks whose embedding model differs from the configured one."""
        updated = 0
        with self.sessions() as session:
            while True:
                batch = session.scalars(
                    select(Chunk)
                    .where(Chunk.embedding_model != self.embedder.model_id)
                    .order_by(Chunk.id)
                    .limit(batch_size)
                ).all()
                if not batch:
                    break
                vectors = self.embedder.embed_documents(
                    [embedding_input(c.text, c.heading) for c in batch]
                )
                for chunk, vector in zip(batch, vectors, strict=True):
                    chunk.embedding = to_blob(vector)
                    chunk.embedding_model = self.embedder.model_id
                session.commit()
                updated += len(batch)
        return updated

    def close(self) -> None:
        self.engine.dispose()


def build_container(settings: Settings, *, generator: Generator | None = None) -> Container:
    engine = create_db_engine(settings.database_url)
    sessions = session_factory(engine)
    embedder = build_embedder(settings)
    retriever = Retriever(
        sessions, embedder, candidate_pool=settings.candidate_pool, rrf_k=settings.rrf_k
    )
    return Container(
        settings=settings,
        engine=engine,
        sessions=sessions,
        embedder=embedder,
        ingestion=IngestionService(sessions, embedder, settings),
        rag=RagService(retriever, generator or build_generator(settings), settings),
    )
