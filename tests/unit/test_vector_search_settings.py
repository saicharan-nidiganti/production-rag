"""Approximate vector search without a database: settings, the index's name and DDL, and the
pgvector version check. tests/integration/test_vector_index.py covers it on PostgreSQL."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from production_rag.cli import main
from production_rag.config import Settings
from production_rag.container import Container
from production_rag.db import vector_index

POSTGRES = "postgresql+pg8000://rag@localhost/rag"


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_approximate_search_needs_postgresql() -> None:
    with pytest.raises(ValidationError, match="RAG_VECTOR_SEARCH=approximate needs PostgreSQL"):
        settings(vector_search="approximate")
    assert settings(database_url=POSTGRES, vector_search="approximate").hnsw_ef_search == 200
    assert settings().vector_search == "exact"


def test_ef_search_must_cover_the_candidates_each_search_returns() -> None:
    with pytest.raises(ValidationError, match="RAG_HNSW_EF_SEARCH must be at least"):
        settings(database_url=POSTGRES, vector_search="approximate", hnsw_ef_search=20)
    settings(database_url=POSTGRES, vector_search="approximate", hnsw_ef_search=30)
    settings(hnsw_ef_search=20)  # ignored while the search is exact
    with pytest.raises(ValidationError):
        settings(hnsw_ef_search=1001)  # pgvector's limit


def test_each_model_has_its_own_stable_index_name() -> None:
    first = vector_index.index_name("fastembed:BAAI/bge-small-en-v1.5")
    assert first == vector_index.index_name("fastembed:BAAI/bge-small-en-v1.5")
    assert first != vector_index.index_name("hashing-v1-d512")
    assert first.startswith("ix_chunks_hnsw_") and len(first) <= 63  # PostgreSQL's limit


def test_the_model_is_quoted_in_the_index_predicate() -> None:
    statement = vector_index._create_statement("ix", "it's", 384, 16, 64)
    assert statement.endswith("WHERE embedding_model = 'it''s'")
    assert "((embedding::vector(384)) vector_ip_ops)" in statement
    assert "CONCURRENTLY" in statement and "WITH (m = 16, ef_construction = 64)" in statement


@pytest.mark.parametrize(
    ("dimensions", "m", "ef", "memory", "message"),
    [
        (0, 16, 64, None, "1 to 2,000 dimensions"),
        (2001, 16, 64, None, "1 to 2,000 dimensions"),
        (384, 1, 64, None, "m must be 2 to 100"),
        (384, 101, 1000, None, "m must be 2 to 100"),
        (384, 40, 64, None, "at least 2 x m"),
        (384, 16, 1001, None, "ef_construction must be 4 to 1,000"),
        (384, 16, 64, "lots", "maintenance_work_mem must look like"),
        (384, 16, 64, "1GB; DROP TABLE chunks", "maintenance_work_mem must look like"),
    ],
)
def test_build_parameters_are_checked(
    dimensions: int, m: int, ef: int, memory: str | None, message: str
) -> None:
    with pytest.raises(vector_index.VectorIndexError, match=message):
        vector_index._check_parameters(dimensions, m, ef, memory)


def test_build_parameters_within_pgvectors_limits_pass() -> None:
    vector_index._check_parameters(384, 16, 64, None)
    vector_index._check_parameters(2000, 100, 1000, "2GB")


class _Connection:
    def __init__(self, version: str | None) -> None:
        self.version = version

    def execute(self, _statement: object) -> _Connection:
        return self

    def scalar(self) -> str | None:
        return self.version


@pytest.mark.parametrize(
    ("installed", "outcome"),
    [
        (None, "isn't installed"),
        ("0.6.0", "needs pgvector 0.8.0 or later"),
        ("0.7.4", "needs pgvector 0.8.0 or later"),
        ("0.8.0", "0.8.0"),
        ("0.8.6", "0.8.6"),
        ("0.10.1", "0.10.1"),
    ],
)
def test_approximate_search_needs_iterative_scans(installed: str | None, outcome: str) -> None:
    conn: Any = _Connection(installed)
    if installed is None or outcome.startswith("needs"):
        with pytest.raises(vector_index.VectorIndexError, match=outcome):
            vector_index.check_pgvector(conn)
    else:
        assert vector_index.check_pgvector(conn) == outcome


def test_the_index_is_refused_on_sqlite(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(vector_index.VectorIndexError, match="for PostgreSQL"):
        vector_index.create("sqlite:///rag.db", "hashing-v1-d512", 512)
    with pytest.raises(vector_index.VectorIndexError, match="for PostgreSQL"):
        vector_index.drop("sqlite:///rag.db", "hashing-v1-d512")
    assert main(["db", "create-vector-index"]) == 1
    assert "for PostgreSQL" in capsys.readouterr().err


def test_exact_search_builds_no_index(container: Container) -> None:
    assert container.settings.vector_search == "exact"
    assert container.build_vector_index() is None
    assert container.embedding_dimensions() == 512
