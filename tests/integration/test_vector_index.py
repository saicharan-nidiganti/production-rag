"""Approximate vector search through an HNSW index (ADR-0014), on PostgreSQL.

These run when RAG_TEST_POSTGRES_URL names a server with pgvector 0.8 or later, as in CI.
Most share one library of three owners: ada with about 3,000 chunks, carol with about 300,
and bob with one small document. Built once per module, it is big enough for the planner to
choose the index, and carol is the case iterative scans exist for: an owner whose chunks are
a small share of what the index scan passes over.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from production_rag.api.app import create_app
from production_rag.cli import main
from production_rag.config import Settings
from production_rag.container import Container, build_container
from production_rag.db import vector_index
from production_rag.db.migrate import upgrade_to_head
from production_rag.evaluation.benchmark import (
    corpus_sentences,
    synthetic_documents,
    vector_recall,
)
from production_rag.evaluation.dataset import load_dataset
from production_rag.metrics import REGISTRY
from tests.conftest import CORPUS_DIR, DATASET, POSTGRES_URL

pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="the HNSW index is PostgreSQL's")

ADA, BOB, CAROL = "ada", "bob", "carol"
K = 30
TIE = 1e-6
RECALL_AT_EF_40 = 0.85  # measured 0.92 (lowest 0.8) on this library, pgvector 0.8.6


def approximate_settings(url: str, **overrides: object) -> Settings:
    # Everything that matters is explicit: module fixtures run before the per-test
    # environment isolation in conftest.py.
    values: dict[str, object] = {
        "database_url": url,
        "embedding_provider": "hashing",
        "reranker": "none",
        "generator": "extractive",
        "vector_search": "approximate",
        "candidate_pool": K,
        "hnsw_ef_search": 200,
        "parse_isolation": "inline",
        "ingest_workers": 0,
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@dataclass
class Library:
    url: str
    container: Container
    questions: list[str]


@pytest.fixture(scope="module")
def library() -> Iterator[Library]:
    assert POSTGRES_URL
    name = f"ragtest_hnsw_{uuid.uuid4().hex[:12]}"
    admin = create_engine(POSTGRES_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(POSTGRES_URL).set(database=name).render_as_string(hide_password=False)
    try:
        upgrade_to_head(url)
        container = build_container(approximate_settings(url))
        try:
            require_iterative_scans(container)
            files = sorted(CORPUS_DIR.glob("*.md"))
            for path in files:
                container.ingestion.ingest(path.name, path.read_bytes(), owner=ADA)
            generated = synthetic_documents(corpus_sentences(files), seed=11)
            for _ in range(56):
                container.ingestion.ingest(*next(generated), owner=ADA)
            for _ in range(20):
                container.ingestion.ingest(*next(generated), owner=CAROL)
            smallest = min(files, key=lambda path: path.stat().st_size)
            container.ingestion.ingest(smallest.name, smallest.read_bytes(), owner=BOB)
            container.build_vector_index()
            questions = [case.question for case in load_dataset(DATASET)]
            yield Library(url, container, questions)
        finally:
            container.close()
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()


def fallbacks() -> float:
    return REGISTRY.get_sample_value("rag_vector_exact_fallbacks_total") or 0.0


def require_iterative_scans(container: Container) -> None:
    with container.engine.connect() as conn:
        version = vector_index.pgvector_version(conn) or ()
    if version < vector_index.MIN_PGVECTOR:
        pytest.skip(f"pgvector {version} has no iterative index scans")


def owners_chunk_ids(container: Container, owner: str) -> set[int]:
    with container.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT chunks.id FROM chunks JOIN documents "
                "ON documents.id = chunks.document_id WHERE documents.owner_id = :owner"
            ),
            {"owner": owner},
        )
        return {int(row[0]) for row in rows}


# On a library this size the planner rightly searches a filtered owner exactly. To test the
# index path it takes on a library many times larger, a test can make every in-query sort
# look expensive: an exact plan needs one to rank chunks, the index plan doesn't.
_PREFER_INDEX = "SET LOCAL enable_sort = off"


def both_searches(
    container: Container,
    question: str,
    owner: str | None,
    k: int = K,
    *,
    prefer_index: bool = False,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    index = container.rag.retriever.vector_index
    query = container.embedder.embed_query(question)
    with container.sessions() as session:
        if prefer_index:
            session.execute(text(_PREFER_INDEX))
        exact = index.search(session, query, k, owner, exact=True)
        approximate = index.search(session, query, k, owner)
    return exact, approximate


def approximate_plan(
    container: Container, question: str, owner: str | None, *, prefer_index: bool = False
) -> list[str]:
    """The plan PostgreSQL chooses for the approximate search as it actually runs."""
    statements: list[tuple[str, object]] = []

    def capture(_conn: object, _cursor: object, statement: str, params: object, *_: object) -> None:
        if "WITH nearest" in statement:
            statements.append((statement, params))

    event.listen(container.engine, "before_cursor_execute", capture)
    try:
        query = container.embedder.embed_query(question)
        with container.sessions() as session:
            container.rag.retriever.vector_index.search(session, query, K, owner)
    finally:
        event.remove(container.engine, "before_cursor_execute", capture)
    assert statements, "the approximate search didn't run"
    statement, params = statements[0]
    with container.engine.begin() as conn:
        if prefer_index:
            conn.execute(text(_PREFER_INDEX))
        return list(conn.exec_driver_sql("EXPLAIN " + statement, params).scalars())  # type: ignore[arg-type]


def uses_index(container: Container, plan: list[str]) -> bool:
    name = vector_index.index_name(container.embedder.model_id)
    return any(f"Index Scan using {name}" in line for line in plan)


def recall(exact: list[tuple[int, float]], approximate: list[tuple[int, float]]) -> float:
    """The share of the exact top k returned; a result scoring the same as the exact k-th
    counts, since equal scores may come in either order."""
    kth = exact[-1][1]
    return min(len(exact), sum(score >= kth - TIE for _, score in approximate)) / len(exact)


# --- The search ---


def test_the_search_can_run_through_the_index(library: Library) -> None:
    """The search orders by the indexed expression under the same model condition, so the
    planner can use the index. Whether it does is its cost estimate: at this size it may
    rightly scan exactly, so the tests that measure the index make its plan the cheaper."""
    container = library.container
    plan = approximate_plan(container, library.questions[0], ADA, prefer_index=True)
    assert uses_index(container, plan), "\n".join(plan)


def test_a_large_owners_results_match_exact_search_closely(library: Library) -> None:
    pairs = [
        both_searches(library.container, question, ADA, prefer_index=True)
        for question in library.questions
    ]
    assert all(len(approximate) == K for _, approximate in pairs)
    recalls = [recall(exact, approximate) for exact, approximate in pairs]
    same_first = [approximate[0][1] >= exact[0][1] - TIE for exact, approximate in pairs]
    assert sum(recalls) / len(recalls) >= 0.95
    assert sum(same_first) / len(same_first) >= 0.95


def test_an_owner_with_part_of_the_library_still_gets_k_of_their_own_results(
    library: Library,
) -> None:
    """The index scan passes over everyone's chunks, and the owner filter comes after it.
    Carol has about a quarter of the library, so with ef_search 40 the first candidates hold
    about ten of hers. An iterative scan keeps going until k of hers have passed."""
    narrow = build_container(approximate_settings(library.url, hnsw_ef_search=40))
    try:
        plan = approximate_plan(narrow, library.questions[0], CAROL, prefer_index=True)
        assert uses_index(narrow, plan), "\n".join(plan)  # else this tests nothing
        carols = owners_chunk_ids(narrow, CAROL)
        assert len(carols) > 4 * K
        before = fallbacks()
        recalls = []
        for question in library.questions[:30]:
            exact, approximate = both_searches(narrow, question, CAROL, prefer_index=True)
            assert len(approximate) == K
            assert {chunk_id for chunk_id, _ in approximate} <= carols
            recalls.append(recall(exact, approximate))
        assert fallbacks() == before  # the index sufficed
        assert sum(recalls) / len(recalls) >= RECALL_AT_EF_40
    finally:
        narrow.close()


def test_an_owner_with_fewer_chunks_than_k_gets_all_of_them_exactly(library: Library) -> None:
    container = library.container
    bobs = owners_chunk_ids(container, BOB)
    assert 0 < len(bobs) < K
    before = fallbacks()
    exact, approximate = both_searches(container, library.questions[0], BOB)
    assert approximate == exact
    assert {chunk_id for chunk_id, _ in approximate} == bobs
    assert fallbacks() == before + 1


def test_no_owner_searches_everyones_chunks(library: Library) -> None:
    exact, approximate = both_searches(
        library.container, library.questions[0], None, prefer_index=True
    )
    assert len(approximate) == K
    assert recall(exact, approximate) >= 0.9


def test_chunks_added_after_the_build_are_in_the_index(library: Library) -> None:
    container = library.container
    text_of_note = (
        "# Greenhouse ventilation\n\nOpen the roof vents when the greenhouse passes 27 degrees "
        "and close them at dusk; the misting pumps run for four minutes every hour."
    )
    added = container.ingestion.ingest("greenhouse.md", text_of_note.encode(), owner=ADA).document
    question = "When do the greenhouse roof vents open?"
    assert uses_index(container, approximate_plan(container, question, ADA, prefer_index=True))
    exact, approximate = both_searches(container, question, ADA, prefer_index=True)
    with container.engine.connect() as conn:
        first = conn.execute(
            text("SELECT document_id FROM chunks WHERE id = :id"), {"id": approximate[0][0]}
        ).scalar_one()
    assert first == added.id
    assert approximate[0] == exact[0]


def test_the_benchmark_measures_the_index_itself(library: Library) -> None:
    """At this size the planner may search exactly; the benchmark's comparison must still
    read the index, or it would compare exact search with itself."""
    cases = load_dataset(DATASET)[:20]
    report = vector_recall(library.container, cases, K)
    assert report["questions"] == 20
    assert report["index_used_rate"] == 1.0
    assert 0.0 <= report["planner_used_index_rate"] <= 1.0
    assert report["recall_mean"] >= 0.9 and report["same_first_rate"] >= 0.9


def test_answers_work_with_approximate_search(library: Library) -> None:
    result = library.container.rag.answer(library.questions[0], owner=ADA)
    assert result.answer.status == "answered"
    assert result.retrieval.chunks


# --- Building, checking and dropping the index ---


def test_create_is_idempotent_rebuilds_an_invalid_index_and_drop_removes_it(
    container: Container,
) -> None:
    url = container.settings.database_url
    model = container.embedder.model_id
    require_iterative_scans(container)
    for path in sorted(CORPUS_DIR.glob("*.md"))[:3]:
        container.ingestion.ingest(path.name, path.read_bytes())

    first = vector_index.create(url, model, 512, m=8, ef_construction=32)
    assert first["created"] is True and first["valid"] is True
    assert (first["dimensions"], first["m"], first["ef_construction"]) == (512, 8, 32)
    assert vector_index.create(url, model, 512)["created"] is False

    # A concurrent build that fails part way leaves an invalid index behind.
    with container.engine.begin() as conn:
        conn.execute(
            text("UPDATE pg_index SET indisvalid = false WHERE indexrelid = CAST(:n AS regclass)"),
            {"n": vector_index.index_name(model)},
        )
    assert vector_index.status(container.engine, model).valid is False
    rebuilt = vector_index.create(url, model, 512)
    assert rebuilt["created"] is True and rebuilt["valid"] is True

    with pytest.raises(vector_index.VectorIndexError, match="has 512 dimensions"):
        vector_index.create(url, model, 384)
    assert vector_index.drop(url, model) is True
    assert vector_index.drop(url, model) is False
    assert vector_index.status(container.engine, model).exists is False


def test_stored_chunks_of_another_dimension_are_refused(container: Container) -> None:
    require_iterative_scans(container)
    container.ingestion.ingest("a.md", b"# A\n\nThe canary receives 5% of traffic.")
    with pytest.raises(vector_index.VectorIndexError, match="have 512 dimensions"):
        vector_index.create(container.settings.database_url, container.embedder.model_id, 384)


def test_readiness_reports_the_index_and_a_missing_one_does_not_block(
    container: Container,
) -> None:
    approximate = build_container(approximate_settings(container.settings.database_url))
    try:
        with TestClient(create_app(container=approximate)) as client:
            body = client.get("/ready").json()
            if "error" in body["checks"]["vector_index"]:
                pytest.skip("pgvector has no iterative index scans")
            assert client.get("/ready").status_code == 200
            assert body["checks"]["vector_index"]["index"] == "missing"
            assert "rag db create-vector-index" in body["checks"]["vector_index"]["hint"]
            approximate.build_vector_index()
            after = client.get("/ready").json()["checks"]["vector_index"]
            assert after["index"] == "valid" and "hint" not in after
    finally:
        approximate.close()


def test_the_cli_creates_and_drops_the_index(
    container: Container, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    require_iterative_scans(container)
    monkeypatch.setenv("RAG_DATABASE_URL", container.settings.database_url)
    assert main(["db", "create-vector-index", "--m", "12", "--memory", "256MB"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["created"] is True and created["m"] == 12
    assert main(["db", "drop-vector-index"]) == 0
    assert json.loads(capsys.readouterr().out)["dropped"] is True
    # Another model's index, by its id: what an operator drops after changing models.
    vector_index.create(container.settings.database_url, "hashing-v1-d512", 512)
    monkeypatch.setenv("RAG_EMBEDDING_DIMENSIONS", "64")  # now configured: hashing-v1-d64
    assert main(["db", "drop-vector-index", "--model", "hashing-v1-d512"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "dropped": True,
        "model": "hashing-v1-d512",
        "index": vector_index.index_name("hashing-v1-d512"),
    }
    assert main(["db", "create-vector-index", "--m", "40", "--ef-construction", "64"]) == 1
    assert "at least 2 x m" in capsys.readouterr().err
