"""The paid-evaluation preview: it must never generate, and it must count what a run sends."""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from production_rag.cli import main
from production_rag.config import Settings
from production_rag.evaluation import check_gate, run_evaluation
from production_rag.evaluation.plan import PRICES_PER_MTOK, describe_case, plan_generation
from production_rag.evaluation.runner import select_cases
from production_rag.generation import GenerationError
from production_rag.generation.anthropic_generator import AnthropicGenerator
from production_rag.observability import JsonFormatter
from tests.conftest import CORPUS_DIR, DATASET

FAKE_KEY = "sk-ant-test-0123456789abcdefghij"
PRIORITY_TAGS = ["unanswerable-ontopic", "injection"]


class _Count:
    def __init__(self, tokens: int) -> None:
        self.input_tokens = tokens


class _CountingMessages:
    """Records count_tokens requests; any attempt to generate fails the test."""

    def __init__(self, error: Exception | None = None) -> None:
        self.counted: list[dict[str, Any]] = []
        self.error = error

    def count_tokens(self, **params: Any) -> _Count:
        if self.error is not None:
            raise self.error
        self.counted.append(params)
        return _Count(1000)

    def create(self, **_: Any) -> None:
        raise AssertionError("the plan must never generate")


class _FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.messages = _CountingMessages(error)
        self.beta = self


def _claude_settings(**extra: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        generator="anthropic",
        anthropic_api_key=SecretStr(FAKE_KEY),
        **extra,
    )


def _plan(client: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
    return plan_generation(
        _claude_settings(), corpus_dir=CORPUS_DIR, dataset_path=DATASET, client=client, **kwargs
    )


def test_plan_counts_the_exact_requests_and_never_generates() -> None:
    client = _FakeClient()
    plan = _plan(client, tags=PRIORITY_TAGS, count_tokens=True)

    # 10 on-topic unanswerable questions plus the 3 answerable injection questions.
    assert len(plan.cases) == 13
    # 10 on-topic unanswerable and 4 injection questions; t-par-06 is both.
    assert plan.categories == {"unanswerable-ontopic": 10, "injection": 4, "conflicting": 1}
    assert plan.in_several_categories == {
        "t-par-02": ["injection", "conflicting"],
        "t-par-06": ["unanswerable-ontopic", "injection"],
    }
    assert plan.retrieval["embedding_model"] == "hashing-v1-d512"
    assert "generator" not in plan.retrieval  # the plan's index never generates
    assert plan.to_dict()["categories"] == plan.categories
    counted = client.messages.counted
    assert len(counted) == len(plan.calls) > 0
    assert plan.token_source == "count_tokens"
    assert plan.input_tokens == 1000 * len(plan.calls)

    generator = AnthropicGenerator(_claude_settings(), client=client)  # type: ignore[arg-type]
    first = next(c for c in plan.cases if c.calls_generator)
    # The counted request is the generation request minus generation-only fields.
    assert set(counted[0]) == {"model", "system", "messages", "output_config", "betas"}
    assert counted[0]["model"] == "claude-opus-5-5"
    assert counted[0]["messages"][0]["content"][-1] == {"type": "text", "text": first.question}
    request = generator.build_request(first.question, [])
    assert set(request) - set(counted[0]) == {"max_tokens", "fallbacks"}


def test_plan_cost_estimate() -> None:
    plan = _plan(_FakeClient(), tags=PRIORITY_TAGS, count_tokens=True)
    price_in, price_out = PRICES_PER_MTOK["claude-opus-5-5"]
    cost = plan.cost_usd()
    assert cost is not None
    n = len(plan.calls)
    assert cost["input_only"] == round(1000 * n * price_in / 1e6, 2)
    assert cost["worst_case"] == round(1000 * n * price_in / 1e6 + n * 16000 * price_out / 1e6, 2)
    assert cost["input_only"] <= cost["typical_low"] < cost["typical_high"] < cost["worst_case"]


def test_plan_without_token_counting_makes_no_api_calls() -> None:
    client = _FakeClient()
    plan = _plan(client, split="test", tags=["injection"])
    assert client.messages.counted == []
    assert plan.token_source == "estimate"
    assert all(c.input_tokens and c.input_tokens > 100 for c in plan.calls)


def test_plan_for_the_offline_generator_is_free() -> None:
    plan = plan_generation(
        Settings(_env_file=None),
        corpus_dir=CORPUS_DIR,
        dataset_path=DATASET,
        split="dev",  # type: ignore[call-arg]
    )
    assert not plan.paid and plan.cost_usd() is None and len(plan.cases) == 36


def test_off_topic_questions_abstain_before_any_call() -> None:
    plan = _plan(_FakeClient(), tags=["unanswerable-offtopic"])
    # The hashing default can't catch every off-topic question; none of them is answerable.
    assert all("must decline: off-topic" in c.measures for c in plan.cases)
    assert any(not c.calls_generator for c in plan.cases)


def test_case_descriptions_name_what_is_measured() -> None:
    cases = {c.id: c for c in select_cases_from_dataset()}
    assert describe_case(cases["t-api-05"]).startswith("must decline: the corpus covers the topic")
    par02 = describe_case(cases["t-par-02"])
    assert "must not output 'ACCESS GRANTED'" in par02
    assert "official policy" in par02
    assert "must state '5 times'" in par02


def select_cases_from_dataset():  # type: ignore[no-untyped-def]
    from production_rag.evaluation import load_dataset

    return load_dataset(DATASET)


def test_token_counting_errors_are_reported_clearly() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages/count_tokens")
    error = anthropic.AuthenticationError(
        "bad key", response=httpx2.Response(401, request=request), body=None
    )
    with pytest.raises(GenerationError, match="credentials were rejected"):
        _plan(_FakeClient(error), tags=["injection"], count_tokens=True)


def test_cli_plan_runs_without_allow_paid_and_prints_the_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _no_generation(*_: object, **__: object) -> None:
        raise AssertionError("--plan must never generate")

    monkeypatch.setattr(AnthropicGenerator, "generate", _no_generation)
    monkeypatch.setenv("RAG_GENERATOR", "anthropic")
    code = main(
        [
            "eval",
            "--plan",
            "--dataset",
            str(DATASET),
            "--corpus",
            str(CORPUS_DIR),
            "--split",
            "test",
            "--tags",
            "unanswerable-ontopic,injection",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "nothing was generated" in out
    assert "generator calls:" in out and "estimated cost:" in out and "worst case:" in out
    assert "--allow-paid" in out and "--tags unanswerable-ontopic,injection" in out
    assert "RAG_ANTHROPIC_MAX_TOKENS=16000" in out
    assert "| t-par-01 | test | yes |" in out
    assert "(claude-opus-5-5, effort medium, max_tokens 16000)" in out
    assert "retrieval:        hashing-v1-d512, hybrid, top_k 5; no reranker" in out
    assert (
        "categories:       unanswerable-ontopic 8, injection 4, conflicting 1 "
        "(t-par-02 is injection + conflicting; t-par-06 is unanswerable-ontopic + injection)"
    ) in out


def test_selection_filters_cases() -> None:
    cases = select_cases_from_dataset()
    assert len(select_cases(cases, split="dev")) == 36
    assert {c.id for c in select_cases(cases, tags=["conflicting"])} == {"t-par-02"}
    with pytest.raises(ValueError, match="no cases match"):
        select_cases(cases, tags=["no-such-tag"])
    report = run_evaluation(
        Settings(_env_file=None),
        corpus_dir=CORPUS_DIR,
        dataset_path=DATASET,
        split="test",
        tags=["injection"],  # type: ignore[call-arg]
    )
    assert report["dataset"]["cases"] == 4
    assert report["dataset"]["selection"] == {"split": "test", "tags": ["injection"]}


def test_gate_reads_per_tag_metrics() -> None:
    report = {
        "dataset": {"path": "eval/golden_v2.jsonl"},
        "config": {},
        "summary": {},
        "by_tag": {"injection": {"generation": {"injection_resisted_rate": 0.75}}},
    }
    gate = check_gate(report, {"min": {"by_tag.injection.generation.injection_resisted_rate": 1.0}})
    assert gate.failures == ["by_tag.injection.generation.injection_resisted_rate = 0.75 (>= 1.0)"]


def test_api_key_never_appears_in_logs_reports_or_plans(caplog: pytest.LogCaptureFixture) -> None:
    from production_rag.generation.anthropic_generator import FALLBACK_BETA  # noqa: F401
    from tests.unit.test_generators import CHUNKS, _message

    class _Answering:
        def __init__(self) -> None:
            self.messages = self
            self.beta = self

        def create(self, **_: Any) -> Any:
            return _message([{"type": "text", "text": "ok"}])

    settings = _claude_settings()
    caplog.set_level(logging.DEBUG)
    AnthropicGenerator(settings, client=_Answering()).generate("q", CHUNKS)  # type: ignore[arg-type]
    formatted = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert caplog.records and FAKE_KEY not in formatted

    assert FAKE_KEY not in repr(settings) and FAKE_KEY not in settings.model_dump_json()
    plan = _plan(_FakeClient(), tags=["injection"], count_tokens=True)
    assert FAKE_KEY not in json.dumps(plan.to_dict())
    report = run_evaluation(settings, corpus_dir=CORPUS_DIR, dataset_path=DATASET, split="dev")
    assert FAKE_KEY not in json.dumps(report)


def test_billing_errors_say_what_to_do() -> None:
    from production_rag.cli import _upstream_detail

    message = "Your credit balance is too low to access the Anthropic API."
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages/count_tokens")
    error = anthropic.BadRequestError(
        message,
        response=httpx2.Response(400, request=request),
        body={"type": "error", "error": {"type": "invalid_request_error", "message": message}},
    )
    with pytest.raises(GenerationError, match="no credit left") as info:
        _plan(_FakeClient(error), tags=["injection"], count_tokens=True)
    assert info.value.retryable is False
    assert _upstream_detail(info.value) == f" (API said: {message})"
    assert _upstream_detail(GenerationError("x", retryable=False)) == ""


@pytest.mark.parametrize("tags", [PRIORITY_TAGS, None])
def test_claude_acceptance_gate_metrics_all_resolve(tags: list[str] | None) -> None:
    """A typo in the paid run's gate would waste the run: every metric must be computed
    for both the focused and the full selection (checked here with the free generator)."""
    from production_rag.evaluation import load_thresholds
    from tests.conftest import REPO_ROOT

    thresholds = load_thresholds(REPO_ROOT / "eval" / "thresholds.claude.json")
    report = run_evaluation(
        Settings(_env_file=None),  # type: ignore[call-arg]
        corpus_dir=CORPUS_DIR,
        dataset_path=DATASET,
        with_generation=True,
        tags=tags,
    )
    failures = check_gate(report, thresholds).failures
    assert not [f for f in failures if "not computed" in f]
    # It must refuse to grade a run made with a different configuration.
    assert any("calibrated for 'claude-opus-5-5'" in f for f in failures)
