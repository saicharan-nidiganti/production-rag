"""The release checklist (scripts/release_checklist.py) and its criteria."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.conftest import REPO_ROOT


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "release_checklist", REPO_ROOT / "scripts" / "release_checklist.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


checklist = _load()
STEPS = {c.step for c in checklist.CRITERIA if c.step}


def all_passed() -> dict[str, Any]:
    return {step: {"outcome": "success", "conclusion": "success"} for step in STEPS}


def states(results: list[dict[str, Any]]) -> dict[str, str]:
    return {r["id"]: r["state"] for r in results}


def test_every_step_passing_is_a_release_with_the_known_gap_stated() -> None:
    results = checklist.evaluate(all_passed())
    assert states(results)["claude"] == "not-measured"
    assert states(results)["image"] == "not-applicable"
    assert {s for i, s in states(results).items() if i not in ("claude", "image")} == {"pass"}
    assert checklist.overall(results) == "pass with warnings"


def test_a_blocking_failure_stops_the_release() -> None:
    steps = all_passed() | {"tests_postgres": {"outcome": "failure"}}
    results = checklist.evaluate(steps)
    assert states(results)["tests-postgres"] == "fail"
    assert checklist.overall(results) == "fail"


@pytest.mark.parametrize("outcome", ["skipped", "cancelled", None])
def test_a_blocking_check_that_didnt_run_fails(outcome: str | None) -> None:
    steps = all_passed()
    if outcome is None:
        del steps["codeql"]
    else:
        steps["codeql"] = {"outcome": outcome}
    results = checklist.evaluate(steps)
    assert states(results)["codeql"] == "fail"
    assert "didn't run" in next(r["detail"] for r in results if r["id"] == "codeql")


def test_a_warning_from_the_performance_gate_is_kept(tmp_path: Path) -> None:
    (tmp_path / "benchmark").mkdir()
    report = {"gate": {"outcome": "warn"}, "query": {"latency_ms": {"retrieval": {"p95": 15.5}}},
              "ingestion": {"chunks": 10002}}  # fmt: skip
    (tmp_path / "benchmark" / "benchmark.json").write_text(json.dumps(report))
    results = checklist.evaluate(all_passed(), tmp_path)
    performance = next(r for r in results if r["id"] == "performance")
    assert performance["state"] == "warn" and "15.5 ms at 10,002 chunks" in performance["detail"]
    assert checklist.overall(results) == "pass with warnings"


def test_evaluation_numbers_come_from_the_reports(tmp_path: Path) -> None:
    (tmp_path / "eval-offline").mkdir()
    (tmp_path / "eval-offline" / "a.json").write_text(
        json.dumps({"summary": {"retrieval": {"hit_rate": 0.947, "mrr": 0.8671}}})
    )
    detail = next(
        r["detail"]
        for r in checklist.evaluate(all_passed(), tmp_path)
        if r["id"] == "retrieval-offline"
    )
    assert detail == "hit rate 0.947, MRR 0.867"


def test_the_cli_writes_both_forms_and_sets_the_exit_code(tmp_path: Path) -> None:
    steps = tmp_path / "steps.json"
    steps.write_text(json.dumps(all_passed()))
    args = [
        "--steps",
        str(steps),
        "--version",
        "1.0.0",
        "--commit",
        "a" * 40,
        "--output",
        str(tmp_path / "out"),
    ]
    assert checklist.main(args) == 0
    written = json.loads((tmp_path / "out" / "checklist.json").read_text())
    assert written["overall"] == "pass with warnings" and len(written["criteria"]) == len(
        checklist.CRITERIA
    )
    markdown = (tmp_path / "out" / "checklist.md").read_text()
    assert markdown.startswith("# Release checklist: 1.0.0") and "not measured" in markdown

    steps.write_text(json.dumps(all_passed() | {"version": {"outcome": "failure"}}))
    assert checklist.main(args) == 1
    steps.write_text("[]")
    assert checklist.main(args) == 2


def test_every_criterion_is_in_the_documentation_and_every_step_in_the_workflow() -> None:
    doc = (REPO_ROOT / "docs" / "release-criteria.md").read_text(encoding="utf-8")
    workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    for criterion in checklist.CRITERIA:
        assert f"`{criterion.id}`" in doc, f"{criterion.id} is missing from release-criteria.md"
        if criterion.step:
            assert re.search(rf"^\s+id: {criterion.step}$", workflow, re.M), (
                f"no step {criterion.step}"
            )


def _notes() -> ModuleType:
    path = REPO_ROOT / "scripts" / "release_notes.py"
    spec = importlib.util.spec_from_file_location("release_notes", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


notes = _notes()


def test_release_notes_come_from_the_versions_section() -> None:
    document = (REPO_ROOT / "docs" / "release.md").read_text(encoding="utf-8")
    title, body = notes.notes_for("0.2.0", document)
    assert title == "Production RAG v0.2.0: streamed answers, DOCX and HTML, and rate limits"
    assert body.startswith("**What's new**") and "> " not in body
    assert "Production RAG v0.1.0" not in body  # stops at the next section


def test_missing_notes_are_an_error(tmp_path: Path) -> None:
    with pytest.raises(notes.NotesError, match=re.escape("no '## v9.9.9 release notes' section")):
        notes.notes_for("9.9.9", "# Releases" + NL)
    with pytest.raises(notes.NotesError, match="no block quote"):
        notes.notes_for("1.0.0", NL.join(["## v1.0.0 release notes", "", "Not quoted."]))


def test_the_notes_cli_writes_the_title_and_appends_the_checklist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = tmp_path / "release.md"
    document.write_text(NL.join(["## v1.0.0 release notes", "", "> **A title.**", ">", "> Body."]))
    checklist = tmp_path / "checklist.md"
    checklist.write_text(NL.join(["# Release checklist: 1.0.0", "", "| x |"]))
    title_file = tmp_path / "title"
    args = ["1.0.0", "--document", str(document), "--checklist", str(checklist)]
    assert notes.main([*args, "--title-file", str(title_file)]) == 0
    assert title_file.read_text(encoding="utf-8") == "A title"
    printed = capsys.readouterr().out
    assert printed.startswith("Body.") and "## Release checklist: 1.0.0" in printed
    assert notes.main(["2.0.0", "--document", str(document)]) == 1


NL = chr(10)
