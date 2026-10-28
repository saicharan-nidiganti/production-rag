import pytest
from sqlalchemy import func, select, text

from production_rag.container import Container, build_container
from production_rag.db.models import Chunk, Document, IndexState
from production_rag.ingestion import (
    DocumentTooLargeError,
    ExtractionError,
    UnsupportedDocumentError,
)
from production_rag.retrieval import lexical_search

GUIDE = b"""# Guide

## Install

Run the installer and accept the defaults.

## Rollback

Use the zebracorn command to undo a broken release.
"""


def _count(container: Container, sql: str) -> int:
    with container.engine.connect() as conn:
        return int(conn.execute(text(sql)).scalar() or 0)


def test_ingest_persists_chunks_with_exact_offsets(container: Container) -> None:
    result = container.ingestion.ingest("guide.md", GUIDE)
    assert result.created
    doc = result.document
    assert (doc.title, doc.content_type, doc.num_chunks) == ("Guide", "text/markdown", 2)
    with container.sessions() as session:
        stored = session.get(Document, doc.id)
        assert stored is not None
        chunks = session.scalars(
            select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.ordinal)
        ).all()
        assert [c.heading for c in chunks] == ["Guide > Install", "Guide > Rollback"]
        for chunk in chunks:
            assert stored.text[chunk.start_char : chunk.end_char] == chunk.text
            assert chunk.embedding_model == container.embedder.model_id
    assert _count(container, "SELECT count(*) FROM chunks_fts") == 2


def test_reingesting_identical_bytes_is_idempotent(container: Container) -> None:
    first = container.ingestion.ingest("guide.md", GUIDE)
    second = container.ingestion.ingest("renamed.md", GUIDE)
    assert not second.created
    assert second.document.id == first.document.id
    assert _count(container, "SELECT count(*) FROM documents") == 1


def test_deleting_a_document_cascades_to_chunks_and_fts(container: Container) -> None:
    doc = container.ingestion.ingest("guide.md", GUIDE).document
    with container.sessions() as session:
        generation_before = session.scalar(select(IndexState.generation))
        session.delete(session.get(Document, doc.id))
        session.commit()
        generation_after = session.scalar(select(IndexState.generation))
        assert lexical_search(session, "zebracorn", 5) == []
    assert _count(container, "SELECT count(*) FROM chunks") == 0
    assert _count(container, "SELECT count(*) FROM chunks_fts") == 0
    assert generation_after == (generation_before or 0) + 2  # one bump per deleted chunk


@pytest.mark.parametrize(
    ("name", "data", "error"),
    [
        ("x.exe", b"MZ", UnsupportedDocumentError),
        ("empty.md", b"   \n\n", ExtractionError),
        ("big.txt", b"a" * 2048, DocumentTooLargeError),
    ],
)
def test_invalid_documents_are_rejected_without_writes(
    container: Container, name: str, data: bytes, error: type[Exception]
) -> None:
    container.settings.max_upload_bytes = 1024
    with pytest.raises(error):
        container.ingestion.ingest(name, data)
    assert _count(container, "SELECT count(*) FROM documents") == 0


@pytest.mark.parametrize("mode", ["vector", "lexical", "hybrid"])
def test_each_mode_finds_the_relevant_section(corpus_container: Container, mode: str) -> None:
    result = corpus_container.rag.search(
        "tessera rollout undo command to roll back", top_k=3, mode=mode
    )  # type: ignore[arg-type]
    assert result.mode == mode
    assert any(
        c.source_name == "deployment-runbook.md" and "Rollback" in (c.heading or "")
        for c in result.chunks
    )
    assert len(result.chunks) <= 3
    if mode == "lexical":
        assert result.best_vector_score is None
        assert all(c.vector_score is None for c in result.chunks)
    else:
        assert result.best_vector_score is not None


def test_vector_cache_sees_new_and_deleted_documents(container: Container) -> None:
    assert container.rag.search("zebracorn", top_k=3, mode="vector").chunks == []
    doc = container.ingestion.ingest("guide.md", GUIDE).document
    hits = container.rag.search("zebracorn undo release", top_k=1, mode="vector").chunks
    assert hits and hits[0].document_id == doc.id
    with container.sessions() as session:
        session.delete(session.get(Document, doc.id))
        session.commit()
    assert container.rag.search("zebracorn", top_k=3, mode="vector").chunks == []


def test_a_second_process_view_stays_consistent(container: Container) -> None:
    """Another container on the same database (as a second worker would) sees new writes."""
    other = build_container(container.settings)
    try:
        assert other.rag.search("zebracorn", top_k=1, mode="vector").chunks == []
        container.ingestion.ingest("guide.md", GUIDE)
        assert other.rag.search("zebracorn", top_k=1, mode="vector").chunks
    finally:
        other.close()


def test_reindex_reembeds_chunks_from_another_model(container: Container) -> None:
    container.ingestion.ingest("guide.md", GUIDE)
    container.settings.embedding_dimensions = 128
    resized = build_container(container.settings)
    try:
        assert resized.ingestion.index_stats()["stale_chunks"] == 2
        assert resized.rag.search("zebracorn", top_k=3, mode="vector").chunks == []
        assert resized.ingestion.reindex() == 2
        assert resized.ingestion.index_stats() == {"documents": 1, "chunks": 2, "stale_chunks": 0}
        assert resized.rag.search("zebracorn", top_k=3, mode="vector").chunks
    finally:
        resized.close()


def test_answer_abstains_before_generation_when_evidence_is_weak(
    corpus_container: Container,
) -> None:
    corpus_container.settings.min_vector_score = 0.99
    result = corpus_container.rag.answer("Which command rolls back a service?")
    assert result.abstained_before_generation
    assert result.answer.status == "insufficient_evidence"
    assert "generation" not in result.timings_ms


def test_answer_with_extractive_generator_cites_retrieved_chunks(
    corpus_container: Container,
) -> None:
    result = corpus_container.rag.answer(
        "How long does the canary receive traffic before the rollout continues?"
    )
    assert result.answer.status == "answered"
    assert "30 minutes" in result.answer.text
    retrieved_ids = {c.chunk_id for c in result.retrieval.chunks}
    assert {c.chunk.chunk_id for c in result.answer.citations} <= retrieved_ids
    assert {"retrieval", "generation", "total"} <= set(result.timings_ms)


def test_index_stats(corpus_container: Container) -> None:
    stats = corpus_container.ingestion.index_stats()
    with corpus_container.sessions() as session:
        chunk_total = session.scalar(select(func.count()).select_from(Chunk))
    assert stats == {"documents": 6, "chunks": chunk_total, "stale_chunks": 0}
