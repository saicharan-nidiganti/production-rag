import json
from pathlib import Path

import pytest

from production_rag.cli import main
from production_rag.config import Settings
from production_rag.db.migrate import head_revision
from production_rag.evaluation import check_gate, load_thresholds, run_evaluation
from tests.conftest import CORPUS_DIR, DATASET, REPO_ROOT

THRESHOLDS = REPO_ROOT / "eval" / "thresholds.json"


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, db_url: str) -> str:
    monkeypatch.setenv("RAG_DATABASE_URL", db_url)
    monkeypatch.setenv("RAG_LOG_LEVEL", "WARNING")
    return db_url


def test_cli_end_to_end(cli_env: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["db", "upgrade"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {
        "current": head_revision(),
        "head": head_revision(),
    }

    assert main(["ingest", str(CORPUS_DIR)]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert len(lines) == 9
    assert all(line["created"] and line["status"] == "ready" for line in lines)

    assert main(["search", "rollback command", "--top-k", "2"]) == 0
    assert "deployment-runbook.md" in capsys.readouterr().out

    assert main(["ask", "How long does the canary run?"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[answered | extractive-v1]") and "30 minutes" in out and "[1]" in out

    assert main(["reindex"]) == 0
    assert json.loads(capsys.readouterr().out)["reembedded_chunks"] == 0


def test_cli_ask_is_readable_without_log_noise(
    cli_env: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("RAG_LOG_LEVEL")  # the default: one-shot commands log warnings only
    main(["db", "upgrade"])
    main(["ingest", str(CORPUS_DIR / "deployment-runbook.md")])
    capsys.readouterr()
    assert main(["ask", "How long does the canary run?"]) == 0
    out, err = capsys.readouterr()
    assert "canary.[1] The canary receives" in out  # markers don't swallow the space
    assert '"event"' not in err and '"event"' not in out


def test_cli_ingest_reports_a_missing_file_and_continues(
    cli_env: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["db", "upgrade"])
    capsys.readouterr()
    missing = tmp_path / "missing.md"
    assert main(["ingest", str(missing), str(CORPUS_DIR / "deployment-runbook.md")]) == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert lines[0]["path"] == str(missing) and "error" in lines[0]
    assert lines[1]["status"] == "ready"


def test_cli_ingest_reports_failures(
    cli_env: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["db", "upgrade"])
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"\xff\xfe")
    assert main(["ingest", str(bad)]) == 1
    assert "UTF-8" in capsys.readouterr().out.splitlines()[-1]


def test_evaluation_report_is_reproducible_and_passes_the_gate() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    first = run_evaluation(settings, corpus_dir=CORPUS_DIR, dataset_path=DATASET)
    second = run_evaluation(settings, corpus_dir=CORPUS_DIR, dataset_path=DATASET)
    assert first["summary"]["retrieval"] == second["summary"]["retrieval"]
    assert first["corpus"]["fingerprint"] == second["corpus"]["fingerprint"]
    assert first["dataset"]["cases"] == 94 and first["corpus"]["documents"] == 9
    assert first["dataset"]["splits"] == {"dev": 36, "test": 58}
    assert len(first["per_query"]) == 94
    assert set(first["summary"]["splits"]) == {"dev", "test"}
    assert "unanswerable-ontopic" in first["by_tag"]
    assert first["config"]["reranker"] == "none"

    gate = check_gate(first, load_thresholds(THRESHOLDS))
    assert gate.passed, gate.failures


def test_evaluation_with_generation_scores_answers() -> None:
    report = run_evaluation(
        Settings(_env_file=None),
        corpus_dir=CORPUS_DIR,
        dataset_path=DATASET,
        with_generation=True,  # type: ignore[call-arg]
    )
    generation = report["summary"]["generation"]
    assert set(generation) == {
        "answered_rate",
        "unanswerable_declined_rate",
        "citation_precision",
        "fact_recall",
        "injection_resisted_rate",
        "failed",
        "failed_ids",
        "truncated",
        "usage",
    }
    assert generation["truncated"] == 0
    assert generation["usage"] == {"input_tokens": 0, "output_tokens": 0}  # offline: unbilled
    assert generation["answered_rate"] > 0.8
    # The extractive baseline never repeats the planted instruction's payload.
    assert generation["injection_resisted_rate"] == 1.0
    assert "generation_p50" in report["summary"]["latency_ms"]


def test_cli_eval_gate_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["eval", "--corpus", str(CORPUS_DIR), "--dataset", str(DATASET)]
    assert main([*args, "--thresholds", str(THRESHOLDS), "--output", str(tmp_path)]) == 0
    assert "gate: passed" in capsys.readouterr().out
    assert len(list(tmp_path.glob("*.json"))) == 1

    strict = tmp_path / "strict.json"
    strict.write_text(json.dumps({"min": {"retrieval.hit_rate": 1.01}}), encoding="utf-8")
    assert main([*args, "--thresholds", str(strict)]) == 1
    assert "gate: FAILED" in capsys.readouterr().out


def test_cli_refuses_paid_generation_eval_without_opt_in(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # .env.example selects the Claude generator; a plain `rag eval --generation` must not
    # silently spend money on every question.
    import production_rag.evaluation as evaluation

    def _must_not_run(*_: object, **__: object) -> None:
        raise AssertionError("evaluation ran without --allow-paid")

    monkeypatch.setattr(evaluation, "run_evaluation", _must_not_run)
    monkeypatch.setenv("RAG_GENERATOR", "anthropic")
    args = ["eval", "--corpus", str(CORPUS_DIR), "--dataset", str(DATASET), "--generation"]
    assert main(args) == 2
    assert "--allow-paid" in capsys.readouterr().err
