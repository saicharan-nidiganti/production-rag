"""`rag bench` (evaluation/benchmark.py): a small but complete run, the workload, the CLI."""

from __future__ import annotations

import hashlib
import io
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from pypdf import PdfReader

from production_rag.cli import main
from production_rag.config import Settings, get_settings
from production_rag.evaluation import benchmark
from production_rag.evaluation.perfgate import lookup
from tests.conftest import CORPUS_DIR

DATASET = CORPUS_DIR.parent / "golden_v2.jsonl"
THRESHOLDS = CORPUS_DIR.parent / "bench_thresholds.json"
SMALL = {"chunks": 150, "repeat": 1, "warmup": 1, "stream_samples": 2, "pdf_pages": 3}


def options(**overrides: Any) -> benchmark.BenchOptions:
    values: dict[str, Any] = {**SMALL, "http_requests": 0, **overrides}
    return benchmark.BenchOptions(corpus_dir=CORPUS_DIR, dataset=DATASET, **values)


@pytest.fixture(scope="module")
def report() -> Iterator[dict[str, Any]]:
    """One small run with every phase, HTTP included, shared by the tests below. Module
    fixtures run before the per-test isolation, so this one clears RAG_* itself."""
    with pytest.MonkeyPatch.context() as patch:
        for key in [k for k in os.environ if k.startswith("RAG_")]:
            patch.delenv(key)
        settings = Settings(_env_file=None, parse_isolation="inline")  # type: ignore[call-arg]
        yield benchmark.run(settings, options(http_requests=8, concurrency=2))


def test_every_phase_is_measured(report: dict[str, Any]) -> None:
    assert report["benchmark_version"] == benchmark.BENCHMARK_VERSION
    assert report["ingestion"]["chunks"] >= 150
    assert report["ingestion"]["documents"] > len(list(CORPUS_DIR.glob("*.md")))
    assert report["ingestion"]["bytes_per_chunk"] > 0
    assert report["pdf"]["pages"] == 3 and report["pdf"]["chunks"] > 0

    query = report["query"]
    assert query["measured"] == query["questions"] == 94
    assert set(query["latency_ms"]) >= {"retrieval", "total"}
    total = query["latency_ms"]["total"]
    assert total["count"] == 94
    assert total["min"] <= total["p50"] <= total["p95"] <= total["p99"] <= total["max"]
    assert sum(query["statuses"].values()) == 94
    assert query["cpu_ms_per_query"] > 0

    assert report["stream"]["first_token_ms"]["count"] == report["stream"]["answered"] > 0
    http = report["http"]
    assert http["errors"] == 0 and http["latency_ms"]["count"] == 8
    assert http["requests_per_s"] > 0
    assert report["tokens"] == {
        "available": False,
        "reason": "the generator reports no token usage",
    }


def test_individual_measurements_are_kept(report: dict[str, Any]) -> None:
    rows = report["measurements"]["query"]
    assert len(rows) == 94 and {"id", "iteration", "status", "timings_ms"} <= rows[0].keys()
    assert len(report["measurements"]["http_ms"]) == 8


def test_the_run_records_what_it_needs_to_be_repeated(report: dict[str, Any]) -> None:
    env, config, work = report["environment"], report["config"], report["workload"]
    assert {"os", "machine", "cpu_count", "python", "sqlite_version", "version"} <= env.keys()
    assert config["embedding_model"] == "hashing-v1-d512" and config["generator"] == "extractive-v1"
    assert work["dataset"]["sha256"] == hashlib.sha256(DATASET.read_bytes()).hexdigest()
    assert work["dataset"]["questions"] == 94 and work["dataset"]["categories"]
    assert work["corpus"]["generator_version"] == benchmark.CORPUS_GENERATOR_VERSION
    assert work["chunks"] == 150 and work["seed"] == 7 and work["concurrency"] == 2


def test_every_committed_rule_finds_its_metric(report: dict[str, Any]) -> None:
    """A renamed metric would silently turn a rule into "not measured"."""
    thresholds = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    offline = {"tokens.per_query", "tokens.estimated_cost_usd"}  # no token usage offline
    for kind in ("limits", "regressions"):
        for metric in thresholds[kind]:
            if metric not in offline:
                assert lookup(report, metric) is not None, f"{kind}: {metric}"


def test_a_baseline_drops_the_individual_measurements(report: dict[str, Any]) -> None:
    baseline = benchmark.without_measurements({**report, "gate": {"outcome": "pass"}})
    assert "measurements" not in baseline and "gate" not in baseline
    assert "first_token_ms_values" not in baseline["stream"]
    assert baseline["query"] == report["query"]


def test_the_summary_shows_the_tail_and_the_gate(report: dict[str, Any]) -> None:
    text = benchmark.render({**report, "gate": {"outcome": "warn", "checks": [], "notes": ["n"]}})
    assert text.startswith("# Benchmark, ")
    assert "**Gate: WARN**" in text and "| Question: total |" in text and "p99" in text
    assert "HTTP, 2 concurrent" in text and "- n" in text


# --- Workload ---


def test_synthetic_documents_are_reproducible_and_distinct() -> None:
    sentences = benchmark.corpus_sentences(sorted(CORPUS_DIR.glob("*.md")))
    assert len(sentences) > 50 and all(s.endswith(".") for s in sentences)

    def first(seed: int, n: int = 3) -> list[tuple[str, bytes]]:
        documents = benchmark.synthetic_documents(sentences, seed)
        return [next(documents) for _ in range(n)]

    assert first(7) == first(7)
    assert first(7) != first(8)
    names, contents = zip(*first(7), strict=True)
    assert names == ("bench-00001.md", "bench-00002.md", "bench-00003.md")
    assert len({hashlib.sha256(c).digest() for c in contents}) == 3
    assert all(len(c) > 40_000 and c.count(b"\n## ") >= 10 for c in contents)


def test_the_generated_pdf_has_text_on_every_page() -> None:
    sentences = benchmark.corpus_sentences(sorted(CORPUS_DIR.glob("*.md")))
    reader = PdfReader(io.BytesIO(benchmark.text_pdf(5, sentences)))
    assert len(reader.pages) == 5
    assert all(len(page.extract_text()) > 500 for page in reader.pages)


def test_the_server_runs_the_same_settings_without_secrets() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key=SecretStr("k" * 24),
        anthropic_api_key=SecretStr("a" * 24),
        top_k=7,
        anthropic_fallbacks=True,
        fastembed_cache_dir="models",
    )
    env = benchmark.server_environment(settings)
    assert Path(env["RAG_FASTEMBED_CACHE_DIR"]) == Path("models").resolve()
    assert env["RAG_TOP_K"] == "7" and env["RAG_ANTHROPIC_FALLBACKS"] == "true"
    assert env["RAG_API_KEY"] == "" and "RAG_ANTHROPIC_API_KEY" not in env
    assert "k" * 24 not in env.values() and "a" * 24 not in env.values()
    assert env["RAG_RATE_LIMIT_QUERY_PER_MINUTE"] == "0" and env["RAG_INGEST_WORKERS"] == "0"
    assert Settings(_env_file=None, **{k[4:].lower(): v for k, v in env.items()  # type: ignore[call-arg]
                    if k.startswith("RAG_") and v}).top_k == 7  # fmt: skip


def test_tokens_and_their_cost_are_estimated_from_the_price_list() -> None:
    tokens = benchmark._tokens(1_000, 200, {"claude-opus-5-5"}, calls=10)
    assert tokens["total"] == 1_200 and tokens["per_query"] == 120
    assert tokens["estimated_cost_usd"] == pytest.approx(1_000 * 4 / 1e6 + 200 * 20 / 1e6)
    unknown = benchmark._tokens(1_000, 200, {"some-other-model"}, calls=10)
    assert unknown["available"] and unknown["estimated_cost_usd"] is None


def test_server_usage_comes_from_its_process_metrics() -> None:
    before = {"process_cpu_seconds_total": 1.0, "process_resident_memory_bytes": 1e8}
    after = {"process_cpu_seconds_total": 3.0, "process_resident_memory_bytes": 2e8}
    usage = benchmark._server_usage(before, after, requests=100)
    assert usage == {"available": True, "cpu_ms_per_request": 20.0, "rss_bytes": 200_000_000}
    assert benchmark._server_usage({}, {}, requests=100)["available"] is False


def test_paid_calls_are_counted_before_anything_runs() -> None:
    assert options(http_requests=100, concurrency=4).model_calls(94) == 1 + 94 + 2 + 108


# --- CLI ---


@pytest.fixture
def cli_args(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv("RAG_LOG_LEVEL", "WARNING")
    small = [f"--{key.replace('_', '-')}={value}" for key, value in SMALL.items()]
    return ["bench", "--corpus", str(CORPUS_DIR), "--dataset", str(DATASET), *small,
            "--requests", "0"]  # fmt: skip


def test_cli_writes_reports_and_a_baseline_then_compares(
    cli_args: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out, baseline = tmp_path / "out", tmp_path / "baseline.json"
    assert main([*cli_args, "--thresholds", str(THRESHOLDS), "--output", str(out),
                 "--save-baseline", str(baseline)]) == 0  # fmt: skip
    first = json.loads((out / "benchmark.json").read_text(encoding="utf-8"))
    assert first["gate"]["outcome"] == "warn"  # no baseline yet
    assert (out / "benchmark.md").read_text(encoding="utf-8").startswith("# Benchmark")
    assert "measurements" not in json.loads(baseline.read_text(encoding="utf-8"))
    assert "**Gate: WARN**" in capsys.readouterr().out

    # Against its own baseline on the same machine, nothing regressed by 2x.
    assert main([*cli_args, "--thresholds", str(THRESHOLDS), "--baseline", str(baseline)]) == 0
    assert "no baseline" not in capsys.readouterr().out


def test_cli_exit_codes_separate_failures_from_errors(
    cli_args: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    impossible = tmp_path / "impossible.json"
    impossible.write_text(json.dumps({"limits": {"query.latency_ms.total.p95": {"max": 0}}}))
    assert main([*cli_args, "--thresholds", str(impossible)]) == 1

    warn_only = tmp_path / "warn.json"
    warn_only.write_text(json.dumps({"limits": {"pdf.size_mb": {"max": 1}}}))  # not measured
    assert main([*cli_args, "--thresholds", str(warn_only)]) == 0
    assert main([*cli_args, "--thresholds", str(warn_only), "--fail-on-warn"]) == 1

    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"limits": {"x": {}}}))
    assert main([*cli_args, "--thresholds", str(broken)]) == 3
    assert main([*cli_args, "--baseline", str(tmp_path / "missing.json")]) == 3
    assert main([*cli_args, "--baseline", str(impossible)]) == 3  # not a benchmark report


def test_cli_refuses_a_paid_run_before_anything_runs(
    cli_args: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def must_not_run(*_: object, **__: object) -> None:
        raise AssertionError("the benchmark ran without --allow-paid")

    monkeypatch.setattr(benchmark, "run", must_not_run)  # a broken guard can't reach the API
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x" * 24)  # nor bill a real key
    monkeypatch.setenv("RAG_GENERATOR", "anthropic")
    get_settings.cache_clear()
    assert main(cli_args) == 2
    assert "paid Claude API calls" in capsys.readouterr().err
