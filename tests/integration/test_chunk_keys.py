"""Stable chunk identifiers (issue #7): what is stable, and what deliberately isn't."""

from __future__ import annotations

import importlib.util

from alembic import command
from sqlalchemy import select, text

from production_rag.config import Settings
from production_rag.container import Container, build_container
from production_rag.db import create_db_engine
from production_rag.db.migrate import MIGRATIONS_DIR, alembic_config
from production_rag.db.models import Chunk, Document
from production_rag.ingestion.chunking import chunk_key
from tests.conftest import CORPUS_DIR

RUNBOOK = (CORPUS_DIR / "deployment-runbook.md").read_bytes()


def _chunks(container: Container) -> list[tuple[int, str, int, int]]:
    """(row id, key, start, end) of every chunk, in document order."""
    with container.sessions() as session:
        rows = session.execute(
            select(Chunk.id, Chunk.chunk_key, Chunk.start_char, Chunk.end_char).order_by(
                Chunk.document_id, Chunk.ordinal
            )
        ).all()
    return [tuple(row) for row in rows]  # type: ignore[misc]


def _delete_all(container: Container) -> None:
    with container.sessions() as session:
        for document in session.scalars(select(Document)).all():
            session.delete(document)
        session.commit()


def test_chunk_key_is_a_deterministic_function_of_content_and_span() -> None:
    key = chunk_key("a" * 64, 10, 250)
    assert key == chunk_key("a" * 64, 10, 250)
    assert len(key) == 32 and int(key, 16) >= 0
    assert key != chunk_key("b" * 64, 10, 250)  # other content
    assert key != chunk_key("a" * 64, 10, 251)  # other span


def test_reingesting_identical_content_keeps_every_key(container: Container) -> None:
    container.ingestion.ingest("deployment-runbook.md", RUNBOOK)
    first = _chunks(container)
    _delete_all(container)
    # Another document arrives first, so the row ids the runbook gets now are different.
    container.ingestion.ingest(
        "api-rate-limits.md", (CORPUS_DIR / "api-rate-limits.md").read_bytes()
    )
    container.ingestion.ingest("renamed-runbook.md", RUNBOOK)  # same bytes, another name
    with container.sessions() as session:
        runbook_id = session.scalar(
            select(Document.id).where(Document.source_name == "renamed-runbook.md")
        )
        second = [
            tuple(row)
            for row in session.execute(
                select(Chunk.id, Chunk.chunk_key, Chunk.start_char, Chunk.end_char)
                .where(Chunk.document_id == runbook_id)
                .order_by(Chunk.ordinal)
            ).all()
        ]

    assert len(first) == len(second) >= 4
    assert [key for _, key, _, _ in second] == [key for _, key, _, _ in first]
    assert [row_id for row_id, *_ in second] != [row_id for row_id, *_ in first]


def test_changed_content_gets_new_keys(container: Container) -> None:
    container.ingestion.ingest("deployment-runbook.md", RUNBOOK)
    original = {key for _, key, _, _ in _chunks(container)}
    _delete_all(container)
    container.ingestion.ingest("deployment-runbook.md", RUNBOOK.replace(b"canary", b"pilot"))
    changed = {key for _, key, _, _ in _chunks(container)}
    assert original.isdisjoint(changed)  # stability is promised only for identical content


def test_other_chunk_settings_change_only_the_keys_of_moved_spans(
    migrated_settings: Settings,
) -> None:
    def keys_by_span(settings: Settings) -> dict[tuple[int, int], str]:
        container = build_container(settings)
        try:
            container.ingestion.ingest("deployment-runbook.md", RUNBOOK)
            return {(start, end): key for _, key, start, end in _chunks(container)}
        finally:
            _delete_all(container)
            container.close()

    default = keys_by_span(migrated_settings)
    smaller = keys_by_span(migrated_settings.model_copy(update={"chunk_size": 300}))
    assert default != smaller
    for span in default.keys() & smaller.keys():  # a span cut identically keeps its key
        assert default[span] == smaller[span]


def test_search_results_carry_the_stable_key(container: Container) -> None:
    container.ingestion.ingest("deployment-runbook.md", RUNBOOK)
    stored = {row_id: key for row_id, key, _, _ in _chunks(container)}
    hits = container.rag.search("rollback command", top_k=3, mode="lexical").chunks
    assert hits and all(hit.chunk_key == stored[hit.chunk_id] for hit in hits)


def _migration_module():  # type: ignore[no-untyped-def]
    path = MIGRATIONS_DIR / "versions" / "0003_stable_chunk_keys.py"
    spec = importlib.util.spec_from_file_location("migration_0003", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_keeps_a_matching_copy_of_the_key_function() -> None:
    frozen = _migration_module().chunk_key
    for args in [("a" * 64, 0, 100), ("0123abcd" * 8, 950, 1950)]:
        assert frozen(*args) == chunk_key(*args)


def test_upgrading_backfills_keys_for_existing_chunks(settings: Settings) -> None:
    config = alembic_config(settings.database_url)
    command.upgrade(config, "0002")
    engine = create_db_engine(settings.database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO documents (id, source_name, title, content_type, content_hash,"
                    " size_bytes, num_chunks, text, metadata, created_at, status, attempts,"
                    " updated_at) VALUES ('d1', 'a.md', 'A', 'text/markdown', :h, 11, 2,"
                    " 'hello world', '{}', '2026-10-29 10:00:00', 'ready', 1,"
                    " '2026-10-29 10:00:00')"
                ),
                {"h": "f" * 64},
            )
            for ordinal, (start, end) in enumerate([(0, 5), (6, 11)]):
                conn.execute(
                    text(
                        "INSERT INTO chunks (document_id, ordinal, text, start_char, end_char,"
                        " embedding, embedding_model) VALUES ('d1', :o, 'x', :s, :e, x'00', 'm')"
                    ),
                    {"o": ordinal, "s": start, "e": end},
                )
        command.upgrade(config, "head")
        with engine.connect() as conn:
            keys = (
                conn.execute(text("SELECT chunk_key FROM chunks ORDER BY ordinal")).scalars().all()
            )
        assert keys == [chunk_key("f" * 64, 0, 5), chunk_key("f" * 64, 6, 11)]
        command.downgrade(config, "0002")  # and back, leaving the chunks in place
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM chunks")).scalar() == 2
    finally:
        engine.dispose()
