"""The committed reference reports back the published numbers (issue #20)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from types import ModuleType
from typing import Any

import pytest

from tests.conftest import DATASET, REPO_ROOT

RESULTS = REPO_ROOT / "eval" / "results" / "2026-10-29"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "reference_results", REPO_ROOT / "scripts" / "reference_results.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it loads
    spec.loader.exec_module(module)
    return module


script = _script()


def _report(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((RESULTS / f"{name}.json").read_text(encoding="utf-8"))
    return data


@pytest.mark.parametrize("configuration", script.CONFIGURATIONS, ids=lambda c: c.name)
def test_each_configuration_has_a_matching_report(configuration: Any) -> None:
    report = _report(configuration.name)
    config = report["config"]
    for key, value in script.expected_config(configuration).items():
        assert config[key] == value, key
    assert report["with_generation"] is False  # offline: nothing generated, nothing paid
    assert report["dataset"]["sha256"] == hashlib.sha256(DATASET.read_bytes()).hexdigest()
    assert report["dataset"]["path"] == "eval/golden_v2.jsonl"
    assert report["corpus"]["path"] == "eval/corpus"  # no machine-specific paths
    assert re.fullmatch(r"[0-9a-f]{40}", report["code"]["git_commit"] or "")


def test_the_index_is_generated_from_the_reports() -> None:
    assert (RESULTS / "README.md").read_text(encoding="utf-8") == script.render_index(RESULTS)


def test_the_readme_results_table_quotes_the_reports() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    rows = {
        "Hashing embeddings, hybrid": "hashing-hybrid",
        "bge-small, hybrid |": "bge-small-hybrid",
        "bge-small, hybrid, reranked": "bge-small-hybrid-rerank5",
    }
    for label, name in rows.items():
        line = next(row for row in readme.splitlines() if row.startswith("| ") and label in row)
        summary = _report(name)["summary"]
        test = summary["splits"]["test"]
        cells = [cell.strip(" *") for cell in line.strip("|").split("|")]
        assert cells[1] == f"{test['retrieval']['hit_rate']:.3f}", label
        assert cells[2] == f"{test['retrieval']['mrr']:.3f}", label
        assert cells[5] == f"{summary['latency_ms']['retrieval_p95']:.0f} ms", label


def test_reports_contain_no_machine_specific_paths() -> None:
    for path in sorted(RESULTS.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"[A-Za-z]:[\\/]|/home/|/Users/|AppData", text), path.name


def test_only_configured_reports_are_committed() -> None:
    expected = {f"{c.name}.json" for c in script.CONFIGURATIONS}
    assert {p.name for p in RESULTS.glob("*.json")} == expected
