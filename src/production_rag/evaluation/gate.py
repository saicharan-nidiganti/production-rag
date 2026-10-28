"""Regression gate: compare an evaluation report against committed thresholds.

Thresholds file::

    {"config": {"embedding_model": "hashing-v1-d512", "retrieval_mode": "hybrid"},
     "min": {"retrieval.hit_rate": 0.9},
     "max": {"abstention.false_abstention_rate": 0.1}}

``config`` records what the thresholds were calibrated for. A report produced with a
different configuration fails the gate instead of being compared with numbers that
don't apply to it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class GateResult:
    failures: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def _lookup(summary: dict[str, Any], dotted: str) -> Any:
    value: Any = summary
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def check_gate(report: dict[str, Any], thresholds: dict[str, Any]) -> GateResult:
    result = GateResult()
    for key, expected in thresholds.get("config", {}).items():
        actual = report["config"].get(key)
        if actual != expected:
            result.failures.append(
                f"config.{key} is {actual!r} but the thresholds were calibrated for {expected!r}"
            )
    for direction in ("min", "max"):
        for metric, bound in thresholds.get(direction, {}).items():
            value = _lookup(report["summary"], metric)
            if value is None:
                result.failures.append(f"{metric}: not computed in this run")
                continue
            ok = value >= bound if direction == "min" else value <= bound
            line = f"{metric} = {value} ({'>=' if direction == 'min' else '<='} {bound})"
            (result.checks if ok else result.failures).append(line)
    return result


def load_thresholds(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data
