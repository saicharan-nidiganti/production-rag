"""Statistics and the performance gate (evaluation/perfgate.py)."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import pytest

from production_rag.evaluation.perfgate import (
    ThresholdsError,
    evaluate,
    lookup,
    percentile,
    summarize,
    validate_thresholds,
)

THRESHOLDS_FILE = Path(__file__).resolve().parents[2] / "eval" / "bench_thresholds.json"


# --- Statistics ---


def test_percentiles_interpolate_between_ranks() -> None:
    values = [float(v) for v in range(100, 0, -1)]  # 1..100, unsorted
    assert percentile(values, 50) == pytest.approx(50.5)
    assert percentile(values, 95) == pytest.approx(95.05)
    assert percentile(values, 99) == pytest.approx(99.01)
    assert percentile(values, 0) == 1 and percentile(values, 100) == 100
    assert percentile([7.0], 99) == 7.0


@pytest.mark.parametrize("q", [-1, 101])
def test_percentiles_outside_0_to_100_are_refused(q: float) -> None:
    with pytest.raises(ValueError, match="outside"):
        percentile([1.0], q)


def test_a_percentile_of_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="no values"):
        percentile([], 50)


def test_summaries_report_the_tail_not_just_the_average() -> None:
    stats = summarize([10.0] * 98 + [500.0, 1000.0])
    assert stats["count"] == 100
    assert stats["p50"] == 10 and stats["mean"] == pytest.approx(24.8)
    assert stats["p99"] == pytest.approx(505.0) and stats["max"] == 1000
    assert summarize([]) == {"count": 0}


def test_lookup_reads_numbers_at_dotted_paths() -> None:
    report = {"query": {"latency_ms": {"total": {"p95": 12.5}}}, "flag": True, "name": "x"}
    assert lookup(report, "query.latency_ms.total.p95") == 12.5
    assert lookup(report, "query.latency_ms.missing.p95") is None
    assert lookup(report, "flag") is None and lookup(report, "name") is None


# --- Thresholds ---


@pytest.mark.parametrize(
    ("thresholds", "message"),
    [
        ([], "JSON object"),
        ({"limits": {"m": {}}}, "numeric min, max"),
        ({"limits": {"m": {"max": "fast"}}}, "numeric min, max"),
        ({"limits": {"m": 5}}, "must be an object"),
        ({"limits": []}, "object of metric rules"),
        ({"regressions": {"m": {"worse": "up", "warn_pct": 1, "fail_pct": 2}}}, "worse must"),
        ({"regressions": {"m": {"worse": "higher", "warn_pct": 0, "fail_pct": 2}}}, "warn_pct"),
        ({"regressions": {"m": {"worse": "higher", "warn_pct": 50, "fail_pct": 20}}}, "below"),
        (
            {
                "regressions": {
                    "m": {"worse": "lower", "warn_pct": 1, "fail_pct": 2, "min_delta": -1}
                }
            },
            "min_delta",
        ),
    ],
)
def test_malformed_thresholds_are_refused(thresholds: Any, message: str) -> None:
    with pytest.raises(ThresholdsError, match=message):
        validate_thresholds(thresholds)


def test_the_committed_thresholds_are_valid() -> None:
    thresholds = json.loads(THRESHOLDS_FILE.read_text(encoding="utf-8"))
    validate_thresholds(thresholds)
    assert thresholds["limits"]["query.latency_ms.retrieval.p95"]["max"] == 300  # §7


# --- The gate ---


def report(**overrides: float) -> dict[str, Any]:
    base: dict[str, Any] = {
        "benchmark_version": 1,
        "environment": {"os": "Linux", "machine": "x86_64", "cpu_count": 4, "python": "3.12"},
        "config": {"embedding_model": "hashing-v1-d512"},
        "workload": {"chunks": 10000},
        "query": {"latency_ms": {"total": {"p95": 10.0}}},
        "http": {"requests_per_s": 100.0, "errors": 0},
    }
    data = copy.deepcopy(base)
    for path, value in overrides.items():
        target = data
        *parents, last = path.split("__")
        for part in parents:
            target = target.setdefault(part, {})
        target[last] = value
    return data


LATENCY = {"query.latency_ms.total.p95": {"worse": "higher", "warn_pct": 25, "fail_pct": 100}}
THROUGHPUT = {"http.requests_per_s": {"worse": "lower", "warn_pct": 20, "fail_pct": 50}}


def outcome_of(
    current: dict[str, Any], baseline: dict[str, Any] | None, rules: dict[str, Any]
) -> str:
    return evaluate(current, {"regressions": rules}, baseline).outcome


def test_limits_pass_fail_and_report_what_isnt_measured() -> None:
    limits = {
        "http.errors": {"max": 0},
        "http.requests_per_s": {"min": 50, "max": 1000},
        "pdf.seconds": {"max": 60},
    }
    gate = evaluate(report(), {"limits": limits})
    assert [c.outcome for c in gate.checks] == ["pass", "pass", "warn"]
    assert gate.outcome == "warn"
    assert "not measured" in gate.checks[2].detail

    failing = evaluate(report(http__errors=3, http__requests_per_s=10), {"limits": limits})
    assert [c.outcome for c in failing.checks[:2]] == ["fail", "fail"]
    assert failing.outcome == "fail"
    assert "above the limit 0" in failing.checks[0].detail
    assert "below the limit 50" in failing.checks[1].detail


def test_without_a_baseline_regressions_are_a_warning_not_a_failure() -> None:
    gate = evaluate(report(), {"regressions": LATENCY})
    assert gate.outcome == "warn" and gate.checks == []
    assert "no baseline" in gate.notes[0]
    assert evaluate(report(), {}).outcome == "pass"


@pytest.mark.parametrize(
    ("p95", "expected"),
    [(9.0, "pass"), (12.0, "pass"), (13.0, "warn"), (19.9, "warn"), (20.0, "fail"), (40.0, "fail")],
)
def test_latency_regressions_by_percent(p95: float, expected: str) -> None:
    current = report(query__latency_ms__total__p95=p95)
    assert outcome_of(current, report(), LATENCY) == expected


@pytest.mark.parametrize(
    ("rps", "expected"), [(150.0, "pass"), (85.0, "pass"), (75.0, "warn"), (50.0, "fail")]
)
def test_throughput_regressions_point_the_other_way(rps: float, expected: str) -> None:
    assert outcome_of(report(http__requests_per_s=rps), report(), THROUGHPUT) == expected


def test_small_absolute_changes_are_noise() -> None:
    rules = {
        "query.latency_ms.total.p95": {**LATENCY["query.latency_ms.total.p95"], "min_delta": 2}
    }
    baseline = report(query__latency_ms__total__p95=0.4)
    assert outcome_of(report(query__latency_ms__total__p95=0.9), baseline, rules) == "pass"
    assert outcome_of(report(query__latency_ms__total__p95=3.0), baseline, rules) == "fail"


def test_growth_from_zero_fails() -> None:
    gate = evaluate(
        report(http__errors=2),
        {"regressions": {"http.errors": {"worse": "higher", "warn_pct": 1, "fail_pct": 2}}},
        report(),
    )
    assert gate.outcome == "fail" and gate.checks[0].change_pct is None


def test_a_metric_missing_from_either_run_is_a_warning() -> None:
    rules = {"pdf.seconds": {"worse": "higher", "warn_pct": 10, "fail_pct": 20}}
    gate = evaluate(report(), {"regressions": rules}, report())
    assert gate.outcome == "warn" and "not measured in this run" in gate.checks[0].detail
    gate = evaluate(report(pdf__seconds=5), {"regressions": rules}, report())
    assert "not measured in the baseline" in gate.checks[0].detail


def test_a_different_workload_or_configuration_isnt_compared() -> None:
    other = report()
    other["workload"] = {"chunks": 500}
    gate = evaluate(report(query__latency_ms__total__p95=50.0), {"regressions": LATENCY}, other)
    assert gate.checks == [] and gate.outcome == "warn"
    assert "different workload" in gate.notes[0]


def test_a_different_machine_only_warns() -> None:
    laptop = report()
    laptop["environment"] = {**laptop["environment"], "os": "Windows", "cpu_count": 16}
    gate = evaluate(report(query__latency_ms__total__p95=50.0), {"regressions": LATENCY}, laptop)
    assert gate.outcome == "warn"
    assert gate.checks[0].outcome == "warn"
    assert "os, cpu_count" in gate.notes[0]


def test_the_gate_serializes_every_check() -> None:
    gate = evaluate(report(query__latency_ms__total__p95=30.0), {"regressions": LATENCY}, report())
    data = gate.as_dict()
    assert data["outcome"] == "fail"
    check = data["checks"][0]
    assert check["current"] == 30.0 and check["baseline"] == 10.0 and check["change_pct"] == 200.0
    assert "200% worse" in check["detail"] and "fail at 100%" in check["detail"]
    assert not math.isnan(check["change_pct"])
