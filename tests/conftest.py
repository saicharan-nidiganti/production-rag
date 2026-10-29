from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from production_rag.api import create_app
from production_rag.config import Settings, get_settings
from production_rag.container import Container, build_container
from production_rag.db.migrate import upgrade_to_head
from production_rag.retrieval import RetrievedChunk

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "eval" / "corpus"
DATASET = REPO_ROOT / "eval" / "golden_v2.jsonl"
DATASET_V1 = REPO_ROOT / "eval" / "golden_v1.jsonl"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    run_live = os.environ.get("RAG_RUN_LIVE_TESTS") == "1"
    run_paid = os.environ.get("RAG_RUN_PAID_TESTS") == "1"
    skip_live = pytest.mark.skip(reason="live test; set RAG_RUN_LIVE_TESTS=1 to run")
    skip_paid = pytest.mark.skip(reason="spends money; set RAG_RUN_PAID_TESTS=1 to run")
    for item in items:
        if "paid" in item.keywords and not run_paid:
            item.add_marker(skip_paid)
        elif "live" in item.keywords and not run_live:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Keep the developer's RAG_* variables and .env file out of tests."""
    for key in list(os.environ):
        if key.startswith("RAG_") and key != "RAG_RUN_LIVE_TESTS":
            monkeypatch.delenv(key)
    # Parse in the test process: a parser process per document would make the suite several
    # times slower. tests/integration/test_parsing_isolation.py and the smoke test cover it.
    monkeypatch.setenv("RAG_PARSE_ISOLATION", "inline")
    monkeypatch.chdir(tmp_path)  # so no stray .env is picked up
    get_settings.cache_clear()
    # The CLI installs a JSON handler on the root logger; restore it so later tests don't
    # log into a capture stream pytest has already closed.
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers, root.level = handlers, level
    get_settings.cache_clear()


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
def settings(db_url: str) -> Settings:
    return Settings(_env_file=None, database_url=db_url)  # type: ignore[call-arg]


@pytest.fixture
def migrated_settings(settings: Settings) -> Settings:
    upgrade_to_head(settings.database_url)
    return settings


@pytest.fixture
def container(migrated_settings: Settings) -> Iterator[Container]:
    c = build_container(migrated_settings)
    yield c
    c.close()


@pytest.fixture
def corpus_container(container: Container) -> Container:
    for path in sorted(CORPUS_DIR.glob("*.md")):
        container.ingestion.ingest(path.name, path.read_bytes())
    return container


@pytest.fixture
def client(container: Container) -> Iterator[TestClient]:
    with TestClient(create_app(container=container)) as test_client:
        yield test_client


def make_chunk(
    chunk_id: int = 1,
    text: str = "The canary receives 5% of production traffic for 30 minutes.",
    *,
    source: str = "deployment-runbook.md",
    heading: str | None = "Deployment Runbook > Canary releases",
    page: int | None = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        chunk_key=f"key-{chunk_id}",
        document_id=f"doc-{chunk_id}",
        document_title="Deployment Runbook",
        source_name=source,
        heading=heading,
        page=page,
        text=text,
        start_char=0,
        end_char=len(text),
        score=1.0,
        vector_score=0.5,
        lexical_score=1.0,
    )
