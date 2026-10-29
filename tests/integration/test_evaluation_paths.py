"""Issue #22: both generation paths through the evaluation, a readable summary, and
per-case failures that are recorded instead of aborting the run.

The hosted path uses the real AnthropicGenerator over a fake client: request building,
citation mapping and error mapping all run, but nothing is sent and nothing is billed.
This shows the pipeline handles Claude's responses; it does not verify Claude's answers.
"""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2
import pytest

from production_rag.cli import main
from production_rag.config import Settings
from production_rag.evaluation import check_gate, load_thresholds, run_evaluation
from production_rag.evaluation.summary import format_summary
from production_rag.generation.anthropic_generator import AnthropicGenerator
from tests.conftest import CORPUS_DIR, DATASET, REPO_ROOT
from tests.unit.test_generators import _citation, _message

_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
OVERLOAD_QUESTION_MARK = "postmortem"  # questions containing this word get an outage


class _ScriptedClaude:
    """Answers every question by citing the first search result; overloads on some."""

    def __init__(self) -> None:
        self.beta = self
        self.messages = self
        self.calls = 0

    def create(self, **request: Any) -> Any:
        self.calls += 1
        question = request["messages"][0]["content"][-1]["text"]
        if OVERLOAD_QUESTION_MARK in question.lower():
            raise anthropic.InternalServerError(
                "overloaded", response=httpx2.Response(529, request=_REQUEST), body=None
            )
        first = request["messages"][0]["content"][0]["content"][0]["text"]
        return _message([{"type": "text", "text": first, "citations": [_citation(0, first[:80])]}])


def _hosted_report(**kwargs: Any) -> tuple[dict[str, Any], _ScriptedClaude]:
    settings = Settings(_env_file=None, generator="anthropic")  # type: ignore[call-arg]
    client = _ScriptedClaude()
    generator = AnthropicGenerator(settings, client=client)  # type: ignore[arg-type]
    report = run_evaluation(
        settings,
        corpus_dir=CORPUS_DIR,
        dataset_path=DATASET,
        with_generation=True,
        generator=generator,
        split="dev",
        **kwargs,
    )
    return report, client


def test_the_hosted_path_scores_answers_and_records_failed_cases() -> None:
    report, client = _hosted_report()
    generation = report["summary"]["generation"]
    failed = [q for q in report["per_query"] if q["generation"]["status"] == "error"]

    assert client.calls > 0  # the generator really ran, against the fake client
    assert failed and generation["failed"] == len(failed)
    assert generation["failed_ids"] == [q["id"] for q in failed]
    for case in failed:  # recorded with the reason, and retrieval still scored
        assert case["generation"]["error"] == "Claude API error (HTTP 529)"
        assert case["generation"]["retryable"] is True
        assert "hit" in case["retrieval"]
    assert generation["answered_rate"] == 1.0  # every completed answerable case cited
    assert generation["usage"]["input_tokens"] == 120 * (client.calls - len(failed))
    assert report["config"]["generator"] == "claude-opus-5-5"


def test_the_acceptance_gate_rejects_a_run_with_failed_cases() -> None:
    report, _ = _hosted_report()
    gate = check_gate(report, {"max": {"generation.failed": 0}})
    assert not gate.passed and gate.failures[0].startswith("generation.failed = ")
    claude_gate = load_thresholds(REPO_ROOT / "eval" / "thresholds.claude.json")
    assert claude_gate["max"] == {"generation.failed": 0}


def test_the_offline_path_has_no_failures_and_bills_nothing() -> None:
    report = run_evaluation(
        Settings(_env_file=None),  # type: ignore[call-arg]
        corpus_dir=CORPUS_DIR,
        dataset_path=DATASET,
        with_generation=True,
        split="dev",
    )
    generation = report["summary"]["generation"]
    assert generation["failed"] == 0 and generation["failed_ids"] == []
    assert generation["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert report["config"]["generator"] == "extractive-v1"


def test_the_summary_is_readable_and_covers_every_split() -> None:
    report, _ = _hosted_report()
    text = format_summary(report)
    lines = text.splitlines()
    assert lines[0].startswith("Dataset   ") and "36 questions (dev 36)" in lines[0]
    assert "Generator claude-opus-5-5" in text and "(not run" not in text
    header = next(line for line in lines if line.strip().startswith("all"))
    assert header.split() == ["all", "dev"]
    for label in ("Hit rate", "MRR", "False abstention", "Answered", "Failed cases"):
        assert any(line.startswith(label) for line in lines), label
    assert "Failed    " in text and "billed" in text


def test_the_cli_prints_the_summary_by_default_and_json_on_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = ["eval", "--corpus", str(CORPUS_DIR), "--dataset", str(DATASET), "--split", "dev"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert out.startswith("Dataset   ") and "Hit rate" in out
    assert main([*args, "--json"]) == 0
    out = capsys.readouterr().out
    assert out.lstrip().startswith("{") and '"summary"' in out
