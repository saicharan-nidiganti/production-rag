import dataclasses
import json
from pathlib import Path

import pytest

from production_rag.evaluation import RelevantSource, check_gate, load_dataset
from production_rag.evaluation.dataset import DatasetError, EvalCase
from production_rag.evaluation.metrics import (
    citation_precision,
    fact_recall,
    forbidden_absent,
    mean,
    percentile,
    score_retrieval,
)
from production_rag.generation import Citation, GeneratedAnswer
from production_rag.text import tokenize
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


def _corpus_sections() -> dict[str, list[tuple[list[str], str]]]:
    """source name -> [(heading path components, section text)] for every corpus document."""
    from production_rag.ingestion import extract
    from tests.conftest import CORPUS_DIR

    sections: dict[str, list[tuple[list[str], str]]] = {}
    for path in sorted(CORPUS_DIR.glob("*.md")):
        doc = extract(path.name, path.read_bytes())
        sections[path.name] = [
            ((sec.heading or "").split(" > "), doc.text[sec.start : sec.end])
            for sec in doc.sections
        ]
    return sections


@pytest.mark.parametrize("dataset", ["golden_v1.jsonl", "golden_v2.jsonl"])
def test_committed_golden_datasets_are_consistent_with_the_corpus(dataset: str) -> None:
    """Labels must point at real sections, and expected facts must be in those sections."""
    from tests.conftest import REPO_ROOT

    corpus = _corpus_sections()
    problems = []
    for case in load_dataset(REPO_ROOT / "eval" / dataset):
        relevant_text = []
        for target in case.relevant:
            if target.source not in corpus:
                problems.append(f"{case.id}: unknown source {target.source}")
                continue
            matching = [
                text
                for heading, text in corpus[target.source]
                if target.heading is None or target.heading in heading
            ]
            if not matching:
                problems.append(f"{case.id}: no section {target.heading!r} in {target.source}")
            relevant_text.extend(matching)
        joined = " ".join(" ".join(relevant_text).split()).lower()
        for fact in case.must_include:
            if case.answerable and fact.lower() not in joined:
                problems.append(f"{case.id}: fact {fact!r} is not in its relevant sections")
    assert problems == []


def test_golden_v2_extends_v1_unchanged() -> None:
    from tests.conftest import DATASET, DATASET_V1

    v1 = {c.id: c for c in load_dataset(DATASET_V1)}
    v2 = {c.id: c for c in load_dataset(DATASET)}
    for case_id, old in v1.items():
        new = v2[case_id]
        assert (new.question, new.relevant, new.must_include) == (
            old.question,
            old.relevant,
            old.must_include,
        )
        assert new.split == "dev"
    assert all(c.split == "test" for cid, c in v2.items() if cid not in v1)


def _leaks(cases: list[EvalCase]) -> list[tuple[str, str]]:
    """Dev/test pairs that repeat a question, or reword one with the same expected answer.

    Templated pairs are fine when the answer differs ("rate limit on the Standard plan"
    against "... on the Free plan"). A reworded copy with the same evidence and facts would
    let calibration on dev inflate the held-out results.
    """

    def answer(case: EvalCase) -> tuple[bool, frozenset[RelevantSource], frozenset[str]]:
        return case.answerable, frozenset(case.relevant), frozenset(case.must_include)

    dev = [c for c in cases if c.split == "dev"]
    test = [c for c in cases if c.split == "test"]
    found = []
    for d in dev:
        for t in test:
            a, b = set(tokenize(d.question)), set(tokenize(t.question))
            similar = len(a & b) / len(a | b) >= 0.5
            same_text = d.question.strip().lower() == t.question.strip().lower()
            if same_text or (similar and answer(d) == answer(t)):
                found.append((d.id, t.id))
    return found


def test_held_out_questions_do_not_leak_from_the_calibration_split() -> None:
    from tests.conftest import DATASET

    cases = load_dataset(DATASET)
    assert len({c.id for c in cases}) == len(cases)
    assert _leaks(cases) == []


def test_the_leak_check_catches_a_reworded_copy() -> None:
    from tests.conftest import DATASET

    cases = load_dataset(DATASET)
    original = next(c for c in cases if c.id == "ret-01")  # application logs: 30 days
    copy = dataclasses.replace(
        original, id="t-copy", split="test", question="For how long are application logs retained?"
    )
    assert _leaks([*cases, copy]) == [("ret-01", "t-copy")]


def test_load_dataset_reads_splits_tags_and_forbidden_strings(tmp_path: Path) -> None:
    path = tmp_path / "d.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "a",
                "question": "q?",
                "split": "test",
                "tags": ["injection"],
                "relevant": [{"source": "s.md"}],
                "must_not_include": ["ACCESS GRANTED"],
            }
        ),
        encoding="utf-8",
    )
    (case,) = load_dataset(path)
    assert (case.split, case.tags, case.must_not_include) == (
        "test",
        ["injection"],
        ["ACCESS GRANTED"],
    )
    path.write_text(json.dumps({"id": "b", "question": "q", "split": "train"}), encoding="utf-8")
    with pytest.raises(DatasetError, match="split"):
        load_dataset(path)


def test_forbidden_absent() -> None:
    assert forbidden_absent("Deliveries are retried 5 times.", ["ACCESS GRANTED"]) == 1.0
    assert forbidden_absent("access granted", ["ACCESS GRANTED"]) == 0.0
    assert forbidden_absent("anything", []) is None


def test_gate_checks_the_dataset_and_split_metrics() -> None:
    report = {
        "dataset": {"path": "eval/golden_v1.jsonl"},
        "config": {},
        "summary": {"splits": {"test": {"retrieval": {"mrr": 0.8}}}},
    }
    result = check_gate(
        report, {"dataset": "golden_v2.jsonl", "min": {"splits.test.retrieval.mrr": 0.9}}
    )
    assert any("calibrated on 'golden_v2.jsonl'" in f for f in result.failures)
    assert "splits.test.retrieval.mrr = 0.8 (>= 0.9)" in result.failures
