"""Statistics and the performance gate for `rag bench` (docs/performance.md).

A thresholds file holds two kinds of rule:

``limits``
    Absolute bounds that apply on any machine, such as the targets in requirements §7::

        "limits": {"query.latency_ms.retrieval.p95": {"max": 300}}

``regressions``
    How far a metric may move against a baseline run before it warns or fails, in percent
    of the baseline, in the direction that is worse. ``min_delta`` ignores changes smaller
    than that absolute amount, so 0.4 ms becoming 0.8 ms isn't reported as +100%::

        "regressions": {"query.latency_ms.total.p95":
                        {"worse": "higher", "warn_pct": 25, "fail_pct": 100, "min_delta": 2}}

Metric paths are dotted paths into the benchmark report. The gate is ``pass``, ``warn`` or
``fail``. A baseline from a different environment turns would-be failures into warnings,
because hardware alone moves these numbers; a baseline from a different workload or
configuration isn't compared at all.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

Outcome = Literal["pass", "warn", "fail"]

# What must match for a baseline to be comparable at all, and for its failures to count.
WORKLOAD_KEYS = ("workload", "config")
ENVIRONMENT_KEYS = ("os", "machine", "cpu_count", "python")


class ThresholdsError(ValueError):
    """The thresholds file is malformed; the message names the rule."""


def percentile(values: Sequence[float], q: float) -> float:
    """The ``q``-th percentile (0-100), interpolating linearly between closest ranks (the
    default of numpy and most spreadsheets)."""
    if not values:
        raise ValueError("no values")
    if not 0 <= q <= 100:
        raise ValueError(f"percentile {q} is outside 0-100")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize(values: Sequence[float]) -> dict[str, float | int]:
    """Count, min, max, mean, p50, p95 and p99, rounded to 3 decimals."""
    if not values:
        return {"count": 0}
    stats = {
        "min": min(values),
        "max": max(values),
        "mean": sum(values) / len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
    }
    return {"count": len(values), **{k: round(v, 3) for k, v in stats.items()}}


def lookup(report: Mapping[str, Any], path: str) -> float | None:
    """The number at a dotted ``path``, or None when it is missing or not a number."""
    value: Any = report
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


@dataclass(frozen=True)
class Check:
    metric: str
    outcome: Outcome
    detail: str
    current: float | None = None
    baseline: float | None = None
    change_pct: float | None = None


@dataclass
class GateReport:
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def outcome(self) -> Outcome:
        outcomes = {check.outcome for check in self.checks}
        if "fail" in outcomes:
            return "fail"
        if "warn" in outcomes or self.notes:
            return "warn"
        return "pass"

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "notes": self.notes,
            "checks": [
                {
                    "metric": c.metric,
                    "outcome": c.outcome,
                    "detail": c.detail,
                    "current": c.current,
                    "baseline": c.baseline,
                    "change_pct": c.change_pct,
                }
                for c in self.checks
            ],
        }


def validate_thresholds(thresholds: Mapping[str, Any]) -> None:
    """Raise ThresholdsError for a rule the gate couldn't apply."""
    if not isinstance(thresholds, Mapping):
        raise ThresholdsError("the thresholds file must hold a JSON object")
    for metric, rule in _rules(thresholds, "limits"):
        bounds = {key: rule[key] for key in ("min", "max") if key in rule}
        if not bounds or not all(_is_number(v) for v in bounds.values()):
            raise ThresholdsError(f"limit {metric}: give a numeric min, max or both")
    for metric, rule in _rules(thresholds, "regressions"):
        if rule.get("worse") not in ("higher", "lower"):
            raise ThresholdsError(f"regression {metric}: worse must be 'higher' or 'lower'")
        for key in ("warn_pct", "fail_pct"):
            if not _is_number(rule.get(key)) or rule[key] <= 0:
                raise ThresholdsError(f"regression {metric}: {key} must be a positive number")
        if rule["fail_pct"] < rule["warn_pct"]:
            raise ThresholdsError(f"regression {metric}: fail_pct is below warn_pct")
        if "min_delta" in rule and (not _is_number(rule["min_delta"]) or rule["min_delta"] < 0):
            raise ThresholdsError(f"regression {metric}: min_delta must be 0 or more")


def evaluate(
    report: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    baseline: Mapping[str, Any] | None = None,
) -> GateReport:
    """Apply the limits, then compare with ``baseline`` when there is one."""
    validate_thresholds(thresholds)
    gate = GateReport()
    for metric, rule in _rules(thresholds, "limits"):
        gate.checks.append(_limit(report, metric, rule))

    regressions = list(_rules(thresholds, "regressions"))
    if baseline is None:
        if regressions:
            gate.notes.append("no baseline: regressions were not checked")
        return gate
    differs = [key for key in WORKLOAD_KEYS if baseline.get(key) != report.get(key)]
    if differs:
        gate.notes.append(
            f"the baseline ran a different {' and '.join(differs)}: regressions were not "
            "checked, since the numbers don't measure the same thing"
        )
        return gate
    environment = report.get("environment", {})
    other = [
        key
        for key in ENVIRONMENT_KEYS
        if baseline.get("environment", {}).get(key) != environment.get(key)
    ]
    if other:
        gate.notes.append(
            f"the baseline ran on a different machine ({', '.join(other)}): regressions are "
            "reported as warnings only"
        )
    for metric, rule in regressions:
        gate.checks.append(_regression(report, baseline, metric, rule, soften=bool(other)))
    return gate


def _limit(report: Mapping[str, Any], metric: str, rule: Mapping[str, Any]) -> Check:
    value = lookup(report, metric)
    if value is None:
        return Check(metric, "warn", "not measured in this run")
    if "max" in rule and value > rule["max"]:
        return Check(metric, "fail", f"{_fmt(value)} is above the limit {_fmt(rule['max'])}", value)
    if "min" in rule and value < rule["min"]:
        return Check(metric, "fail", f"{_fmt(value)} is below the limit {_fmt(rule['min'])}", value)
    bounds = " and ".join(f"{k} {_fmt(rule[k])}" for k in ("min", "max") if k in rule)
    return Check(metric, "pass", f"{_fmt(value)} is within {bounds}", value)


def _regression(
    report: Mapping[str, Any],
    baseline: Mapping[str, Any],
    metric: str,
    rule: Mapping[str, Any],
    *,
    soften: bool,
) -> Check:
    current, before = lookup(report, metric), lookup(baseline, metric)
    if current is None or before is None:
        missing = "this run" if current is None else "the baseline"
        return Check(metric, "warn", f"not measured in {missing}", current, before)
    worse_by = current - before if rule["worse"] == "higher" else before - current
    change = (current - before) / before * 100 if before else None
    change_pct = round(change, 1) if change is not None else None
    if worse_by <= rule.get("min_delta", 0):
        detail = "better" if worse_by < 0 else "within the noise allowance"
        return Check(metric, "pass", f"{_fmt(before)} -> {_fmt(current)}, {detail}",
                     current, before, change_pct)  # fmt: skip
    worse_pct = math.inf if not before else worse_by / abs(before) * 100
    outcome: Outcome = "pass"
    if worse_pct >= rule["fail_pct"]:
        outcome = "warn" if soften else "fail"
    elif worse_pct >= rule["warn_pct"]:
        outcome = "warn"
    detail = f"{_fmt(before)} -> {_fmt(current)}, {worse_pct:.0f}% worse"
    if outcome != "pass":
        detail += f" (warn at {rule['warn_pct']}%, fail at {rule['fail_pct']}%)"
    return Check(metric, outcome, detail, current, before, change_pct)


def _rules(thresholds: Mapping[str, Any], kind: str) -> Iterable[tuple[str, Mapping[str, Any]]]:
    rules = thresholds.get(kind, {})
    if not isinstance(rules, Mapping):
        raise ThresholdsError(f"{kind} must be an object of metric rules")
    for metric, rule in rules.items():
        if not isinstance(rule, Mapping):
            raise ThresholdsError(f"{kind} {metric}: the rule must be an object")
        yield metric, rule


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _fmt(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 1000 else f"{value:.3g}"
