"""Where PostgreSQL stores each chunk's search vector (migration 0006, ADR-0015): in the row,
so keyword search doesn't fetch it from TOAST storage for every match, and the command that
rewrites rows written before the migration."""

from __future__ import annotations

import json

import pytest
from alembic import command
from sqlalchemy import Engine, text

from production_rag.cli import main
from production_rag.config import Settings
from production_rag.container import Container
from production_rag.db import create_db_engine
from production_rag.db import maintenance as m
from production_rag.db.migrate import alembic_config
from production_rag.retrieval import lexical_search
from tests.conftest import CORPUS_DIR, POSTGRES_URL

pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TOAST storage is PostgreSQL's")


def storage(engine: Engine) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text(
                    "SELECT attstorage FROM pg_attribute "
                    "WHERE attrelid = 'chunks'::regclass AND attname = 'text_search'"
                )
            ).scalar_one()
        )


def test_the_migration_keeps_search_vectors_in_the_row(settings: Settings) -> None:
    config = alembic_config(settings.database_url)
    command.upgrade(config, "head")
    engine = create_db_engine(settings.database_url)
    try:
        assert storage(engine) == "m"  # MAIN: in the row, moved out only as a last resort
        command.downgrade(config, "0005")
        assert storage(engine) == "x"  # EXTENDED, PostgreSQL's default
        command.upgrade(config, "head")
        assert storage(engine) == "m"
    finally:
        engine.dispose()


def ingest_corpus(container: Container) -> None:
    for path in sorted(CORPUS_DIR.glob("*.md")):
        container.ingestion.ingest(path.name, path.read_bytes())


def row_versions(container: Container) -> dict[int, str]:
    with container.engine.connect() as conn:
        return {int(r.id): str(r.ctid) for r in conn.execute(text("SELECT id, ctid FROM chunks"))}


def search(container: Container) -> list[tuple[int, float]]:
    with container.sessions() as session:
        return lexical_search(session, "How long does the canary run before rollout?", 10, "")


def test_the_rewrite_rewrites_every_chunk_and_changes_no_result(container: Container) -> None:
    ingest_corpus(container)
    before, versions = search(container), row_versions(container)
    result = m.rewrite_search_vectors(container.settings.database_url, batch_size=7)
    assert result["rewritten_chunks"] == len(versions)
    after = row_versions(container)
    assert after.keys() == versions.keys()
    assert all(after[i] != versions[i] for i in versions)  # a new row version for each
    assert search(container) == before


def test_the_rewrite_refuses_while_an_hnsw_index_exists(
    container: Container, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ingest_corpus(container)
    with container.engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX ix_chunks_hnsw_test ON chunks USING hnsw "
                "((embedding::vector(512)) vector_ip_ops) WHERE embedding_model = 'hashing-v1-d512'"
            )
        )
    monkeypatch.setenv("RAG_DATABASE_URL", container.settings.database_url)
    assert main(["db", "rebuild-search-index"]) == 1
    assert "drop-vector-index" in capsys.readouterr().err
    assert main(["db", "rebuild-search-index", "--keep-vector-index"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["rebuilt"] == "search_vectors" and printed["rewritten_chunks"] > 0


def test_the_rewrite_is_for_postgresql_only() -> None:
    with pytest.raises(m.MaintenanceError, match="for PostgreSQL"):
        m.rewrite_search_vectors("sqlite:///rag.db")
