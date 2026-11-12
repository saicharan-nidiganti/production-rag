"""The release checklist (docs/release-criteria.md): every criterion, its state, and whether
the release may go ahead.

The release workflow runs each check as a step and passes the steps' outcomes here, as
GitHub Actions' ``toJSON(steps)``. A criterion is:

- ``pass``: its step succeeded;
- ``fail``: a blocking step failed, or didn't run;
- ``warn``: a non-blocking step failed, or the performance gate warned;
- ``not-measured``: it can't be checked for this release, for the stated reason;
- ``not-applicable``: another workflow enforces it.

The release fails if any criterion fails. Exit status: 0 when the release may go ahead, 1 when
it may not, 2 for bad input.

    python scripts/release_checklist.py --steps steps.json --version 1.0.0 --commit SHA \\
        --reports reports/ --output reports/
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

State = Literal["pass", "fail", "warn", "not-measured", "not-applicable", "skipped"]


@dataclass(frozen=True)
class Criterion:
    id: str
    category: str
    title: str
    step: str | None  # the workflow step that checks it; None when nothing runs
    blocking: bool = True
    fixed_state: State | None = None  # for criteria no step checks
    reason: str = ""


CRITERIA: tuple[Criterion, ...] = (
    Criterion(
        "version",
        "Reproducibility",
        "The tag, pyproject.toml and the package agree on the version, and the changelog has a "
        "dated section for it",
        "version",
    ),
    Criterion(
        "lock",
        "Reproducibility",
        "uv.lock matches pyproject.toml, so the release installs exactly what was tested",
        "lock",
    ),
    Criterion(
        "ci",
        "Reproducibility",
        "CI passed on the tagged commit: three Python versions, PostgreSQL, the web interface, "
        "the benchmark",
        "ci",
    ),
    Criterion("lint", "Functional", "Ruff lint and format", "lint"),
    Criterion("types", "Functional", "mypy --strict over the package and scripts", "types"),
    Criterion(
        "tests-sqlite",
        "Functional",
        "The test suite on SQLite, with the 90% coverage floor",
        "tests",
    ),
    Criterion(
        "tests-postgres",
        "Functional",
        "The test suite on PostgreSQL with pgvector, including the isolation suite",
        "tests_postgres",
    ),
    Criterion(
        "web",
        "Functional",
        "The web interface: ESLint with accessibility rules, tsc, tests, the build",
        "web",
    ),
    Criterion(
        "smoke",
        "Functional",
        "A real server: health, ingestion, search, a cited answer, a stream, metrics, an online "
        "backup",
        "smoke",
    ),
    Criterion(
        "retrieval-offline",
        "Retrieval",
        "Retrieval gate, offline configuration, on SQLite (eval/thresholds.json)",
        "eval_offline",
    ),
    Criterion(
        "retrieval-postgres",
        "Retrieval",
        "Retrieval gate, offline configuration, on PostgreSQL (eval/thresholds.json)",
        "eval_postgres",
    ),
    Criterion(
        "retrieval-recommended",
        "Retrieval",
        "Retrieval gate, recommended configuration: bge-small and reranking "
        "(eval/thresholds.reranked.json)",
        "eval_reranked",
    ),
    Criterion(
        "citations",
        "Generation and citations",
        "Answer assembly and citation mapping, with the extractive generator "
        "(eval/thresholds.generation.json)",
        "eval_generation",
    ),
    Criterion(
        "claude",
        "Generation and citations",
        "Claude's answers: declining unanswerable questions, resisting injection, citations "
        "(eval/thresholds.claude.json)",
        None,
        blocking=False,
        fixed_state="not-measured",
        reason=(
            "The Anthropic account has no API credit. Accepted by the owner for 1.0.0 on "
            "2026-11-12, on condition the release notes say so; later releases state it the "
            "same way until the evaluation runs, and the run is prepared (docs/evaluation.md)."
        ),
    ),
    Criterion(
        "performance",
        "Performance",
        "Performance gate: the requirements' limits, and regressions against the CI baseline "
        "(eval/bench_thresholds.json)",
        "bench",
    ),
    Criterion(
        "vector-index",
        "Performance",
        "Approximate vector search on PostgreSQL at 10,000 chunks: recall against exact "
        "search, and retrieval latency (eval/bench_thresholds.approximate.json)",
        "bench_approximate",
    ),
    Criterion(
        "audit",
        "Security",
        "No known vulnerability in a locked Python dependency (pip-audit) or a browser package "
        "(npm audit)",
        "audit",
    ),
    Criterion(
        "secrets", "Security", "No secrets in the repository's history (gitleaks)", "secrets"
    ),
    Criterion("codeql", "Security", "No open code-scanning alerts (CodeQL)", "codeql"),
    Criterion(
        "image",
        "Deployment",
        "The container image is built, run, checked and scanned before it is published",
        None,
        blocking=False,
        fixed_state="not-applicable",
        reason=(
            "Enforced by the container workflow on the same tag, which refuses to publish "
            "an image that fails."
        ),
    ),
    Criterion(
        "package",
        "Deployment",
        "The wheel and sdist build; the wheel installs on its own and runs the CLI",
        "package",
    ),
    Criterion(
        "reproducible",
        "Deployment",
        "Building twice from the tag gives byte-identical wheel and sdist",
        "reproducible",
    ),
    Criterion(
        "docs",
        "Documentation",
        "Markdown lint; links, settings and the changelog are checked by the test suite",
        "docs",
    ),
)


def evaluate(steps: Mapping[str, Any], reports: Path | None = None) -> list[dict[str, Any]]:
    """Each criterion with its state and a detail line."""
    results = []
    for criterion in CRITERIA:
        state: State
        detail = criterion.reason
        if criterion.fixed_state is not None:
            state = criterion.fixed_state
        else:
            outcome = (steps.get(criterion.step or "") or {}).get("outcome")
            if outcome == "success":
                state = "pass"
            elif outcome == "failure":
                state = "fail" if criterion.blocking else "warn"
                detail = "the check failed; see its step in the workflow log"
            elif criterion.blocking:
                state = "fail"
                detail = f"the check didn't run ({outcome or 'no such step'})"
            else:
                state = "skipped"
                detail = f"the check didn't run ({outcome or 'no such step'})"
            if reports is not None:
                state, detail = _refine(criterion, state, detail, reports)
        results.append(
            {
                "id": criterion.id,
                "category": criterion.category,
                "title": criterion.title,
                "blocking": criterion.blocking,
                "state": state,
                "detail": detail,
            }
        )
    return results


def _refine(criterion: Criterion, state: State, detail: str, reports: Path) -> tuple[State, str]:
    """Numbers from the reports, and the benchmark gate's warning."""
    if criterion.id == "performance":
        report = _load(reports / "benchmark" / "benchmark.json")
        if report:
            gate = report.get("gate", {}).get("outcome")
            p95 = report.get("query", {}).get("latency_ms", {}).get("retrieval", {}).get("p95")
            detail = (
                f"gate {gate}; retrieval p95 {p95} ms at {report['ingestion']['chunks']:,} chunks"
            )
            if state == "pass" and gate == "warn":
                state = "warn"
    if criterion.id == "vector-index":
        report = _load(reports / "benchmark-approximate" / "benchmark.json")
        if report:
            recall = report.get("query", {}).get("vector_recall", {})
            p95 = report.get("query", {}).get("latency_ms", {}).get("retrieval", {}).get("p95")
            detail = (
                f"recall {recall.get('recall_mean')}, same first result "
                f"{recall.get('same_first_rate')}; retrieval p95 {p95} ms at "
                f"{report['ingestion']['chunks']:,} chunks"
            )
    folders = {
        "retrieval-offline": "eval-offline",
        "retrieval-postgres": "eval-postgres",
        "retrieval-recommended": "eval-reranked",
        "citations": "eval-generation",
    }
    if criterion.id in folders:
        report = _newest(reports / folders[criterion.id])
        if report:
            summary = report["summary"]
            if criterion.id == "citations":
                generation = summary.get("generation", {})
                detail = (
                    f"answered {generation.get('answered_rate')}, citation precision "
                    f"{generation.get('citation_precision')}, "
                    f"fact recall {generation.get('fact_recall')}"
                )
            else:
                retrieval = summary["retrieval"]
                detail = f"hit rate {retrieval['hit_rate']:.3f}, MRR {retrieval['mrr']:.3f}"
    return state, detail


def _load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _newest(folder: Path) -> dict[str, Any] | None:
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    return _load(files[-1]) if files else None


def overall(results: list[dict[str, Any]]) -> str:
    states = {result["state"] for result in results}
    if "fail" in states:
        return "fail"
    if states & {"warn", "not-measured", "skipped"}:
        return "pass with warnings"
    return "pass"


def render(checklist: Mapping[str, Any]) -> str:
    icon = {"pass": "pass", "fail": "**FAIL**", "warn": "warn", "not-measured": "not measured",
            "not-applicable": "n/a", "skipped": "skipped"}  # fmt: skip
    lines = [
        f"# Release checklist: {checklist['version']}",
        "",
        f"**{checklist['overall'].upper()}** at `{checklist['commit'][:12]}`, "
        f"{checklist['created_at'][:19].replace('T', ' ')} UTC",
        "",
        "| Category | Criterion | Blocking | State | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in checklist["criteria"]:
        blocking = "yes" if r["blocking"] else "no"
        detail = r["detail"].replace("|", "\\|")
        lines.append(
            f"| {r['category']} | {r['title']} | {blocking} | {icon[r['state']]} | {detail} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--steps", required=True, help="toJSON(steps) from the workflow")
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--reports", help="the folder the workflow wrote its reports to")
    parser.add_argument("--output", help="write checklist.json and checklist.md here")
    args = parser.parse_args(argv)
    try:
        steps = json.loads(Path(args.steps).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: can't read {args.steps}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(steps, dict):
        print("error: --steps must hold a JSON object of steps", file=sys.stderr)
        return 2
    results = evaluate(steps, Path(args.reports) if args.reports else None)
    checklist = {
        "version": args.version,
        "commit": args.commit,
        "created_at": datetime.now(UTC).isoformat(),
        "overall": overall(results),
        "criteria": results,
    }
    text = render(checklist)
    if args.output:
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / "checklist.json").write_text(
            json.dumps(checklist, indent=2) + "\n", encoding="utf-8"
        )
        (out / "checklist.md").write_text(text, encoding="utf-8")
    print(text, end="")
    return 1 if checklist["overall"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
