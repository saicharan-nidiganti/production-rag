import json
from pathlib import Path

import pytest

from production_rag.evaluation import RelevantSource, check_gate, load_dataset
from production_rag.evaluation.dataset import DatasetError
from production_rag.evaluation.metrics import (
    citation_precision,
    fact_recall,
    mean,
    percentile,
    score_retrieval,
)
from production_rag.generation import Citation, GeneratedAnswer
from tests.conftest import make_chunk

CANARY = RelevantSource("deployment-runbook.md", "Canary releases")
ROLLBACK = RelevantSource("deployment-runbook.md", "Rollback")


def test_relevant_source_matches_document_and_heading_component() -> None:
    chunk = make_chunk(heading="Deployment Runbook > Canary releases")
    assert CANARY.matches(chunk)
    assert RelevantSource("deployment-runbook.md").matches(chunk)
    assert not RelevantSource("deployment-runbook.md", "Canary").matches(chunk)  # whole component
    assert not RelevantSource("other.md").matches(chunk)
    assert not CANARY.matches(make_chunk(heading=None))


def test_score_retrieval() -> None:
    retrieved = [
        make_chunk(1, heading="Deployment Runbook > Rollback"),
        make_chunk(2, heading="Deployment Runbook > Canary releases"),
        make_chunk(3, heading="Deployment Runbook > Release pipeline"),
    ]
    scores = score_retrieval(retrieved, [CANARY, RelevantSource("platform-overview.md")])
    assert scores.hit == 1.0
    assert scores.first_relevant_rank == 2
    assert scores.reciprocal_rank == 0.5
    assert scores.recall == 0.5
    assert scores.precision == pytest.approx(1 / 3)
    empty = score_retrieval([], [CANARY])
    assert (empty.hit, empty.reciprocal_rank, empty.precision) == (0.0, 0.0, 0.0)


def test_citation_precision_and_fact_recall() -> None:
    good = Citation(make_chunk(1, heading="X > Canary releases"), "a")
    bad = Citation(make_chunk(2, heading="X > Other"), "b")
    answer = GeneratedAnswer("answered", [], [good, bad], model="m")
    assert citation_precision(answer, [CANARY]) == 0.5
    assert citation_precision(GeneratedAnswer("answered", [], [], model="m"), [CANARY]) is None
    assert fact_recall(
        "It runs for 30 Minutes at 5%.", ["30 minutes", "5%", "25%"]
    ) == pytest.approx(2 / 3)
    assert fact_recall("anything", []) is None


def test_mean_and_percentile() -> None:
    assert mean([1.0, None, 3.0]) == 2.0
    assert mean([None]) is None
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 95.0
    assert percentile([7.0], 99) == 7.0
    assert percentile([], 50) is None


def _report(**summary: object) -> dict[str, object]:
    return {"config": {"embedding_model": "hashing-v1-d512"}, "summary": summary}


def test_gate_passes_and_fails_on_bounds() -> None:
    report = _report(retrieval={"hit_rate": 0.95}, abstention={"false_abstention_rate": 0.2})
    thresholds = {
        "min": {"retrieval.hit_rate": 0.9},
        "max": {"abstention.false_abstention_rate": 0.1},
    }
    result = check_gate(report, thresholds)
    assert not result.passed
    assert result.checks == ["retrieval.hit_rate = 0.95 (>= 0.9)"]
    assert result.failures == ["abstention.false_abstention_rate = 0.2 (<= 0.1)"]


def test_gate_fails_on_missing_metric_and_config_mismatch() -> None:
    report = _report(retrieval={"hit_rate": 1.0})
    thresholds = {
        "config": {"embedding_model": "fastembed:x"},
        "min": {"generation.fact_recall": 0.5},
    }
    failures = check_gate(report, thresholds).failures
    assert any("calibrated for 'fastembed:x'" in f for f in failures)
    assert any("generation.fact_recall: not computed" in f for f in failures)


def test_load_dataset(tmp_path: Path) -> None:
    path = tmp_path / "d.jsonl"
    path.write_text(
        json.dumps(
            {"id": "a", "question": "q?", "relevant": [{"source": "s.md"}], "must_include": ["x"]}
        )
        + "\n\n"
        + json.dumps({"id": "b", "question": "unanswerable?"})
        + "\n",
        encoding="utf-8",
    )
    cases = load_dataset(path)
    assert [c.answerable for c in cases] == [True, False]
    assert cases[0].relevant == [RelevantSource("s.md")]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"id": "a", "question": "q"}\n{"id": "a", "question": "q"}\n', "duplicate"),
        ("{not json}\n", "invalid case"),
        ('{"question": "missing id"}\n', "invalid case"),
        ("\n", "empty"),
    ],
)
def test_load_dataset_rejects_bad_files(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "d.jsonl"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(DatasetError, match=message):
        load_dataset(path)


def test_committed_golden_dataset_is_valid() -> None:
    from tests.conftest import CORPUS_DIR, DATASET

    cases = load_dataset(DATASET)
    sources = {p.name for p in CORPUS_DIR.glob("*.md")}
    for case in cases:
        for target in case.relevant:
            assert target.source in sources, f"{case.id} names unknown source {target.source}"
