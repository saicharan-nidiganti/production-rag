import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from production_rag.config import Settings, get_settings
from production_rag.observability import JsonFormatter, Timer, request_id_var


def test_settings_read_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_TOP_K", "7")
    monkeypatch.setenv("RAG_GENERATOR", "anthropic")
    monkeypatch.setenv("RAG_API_KEY", "s3cret-s3cret-s3cret")
    settings = get_settings()
    assert settings.top_k == 7
    assert settings.generator == "anthropic"
    assert settings.api_key is not None
    assert settings.api_key.get_secret_value() == "s3cret-s3cret-s3cret"
    assert "s3cret-s3cret-s3cret" not in repr(settings)


def test_empty_values_in_dotenv_mean_unset(tmp_path: Path) -> None:
    # Regression: `RAG_API_KEY=` (as shipped in .env.example) used to enable auth with an
    # empty key, so every request was rejected or an empty X-API-Key header was accepted.
    env = tmp_path / ".env"
    env.write_text("RAG_API_KEY=\nANTHROPIC_API_KEY=\nRAG_MIN_VECTOR_SCORE=\n", encoding="utf-8")
    settings = Settings(_env_file=env)  # type: ignore[call-arg]
    assert settings.api_key is None
    assert settings.anthropic_api_key is None
    assert settings.min_vector_score is None


def test_short_api_keys_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_API_KEY", "short")
    with pytest.raises(ValidationError, match="at least 16 characters"):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "RAG_ANTHROPIC_API_KEY"])
def test_anthropic_key_is_read_from_dotenv(tmp_path: Path, name: str) -> None:
    # Regression: the SDK only reads the process environment, so a key placed in .env was
    # silently ignored. Settings now read it and pass it to the client.
    env = tmp_path / ".env"
    env.write_text(f"{name}=sk-test-from-dotenv\n", encoding="utf-8")
    settings = Settings(_env_file=env)  # type: ignore[call-arg]
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-test-from-dotenv"

    from production_rag.generation.anthropic_generator import AnthropicGenerator

    generator = AnthropicGenerator(settings)
    assert generator._client.api_key == "sk-test-from-dotenv"


def test_chunk_overlap_must_be_smaller_than_size() -> None:
    with pytest.raises(ValidationError, match="chunk_overlap"):
        Settings(_env_file=None, chunk_size=200, chunk_overlap=200)  # type: ignore[call-arg]


def test_abstain_threshold_defaults_per_embedder_and_can_be_overridden() -> None:
    assert Settings(_env_file=None).abstain_threshold == 0.05  # type: ignore[call-arg]
    assert Settings(_env_file=None, embedding_provider="fastembed").abstain_threshold == 0.55  # type: ignore[call-arg]
    assert Settings(_env_file=None, min_vector_score=0.3).abstain_threshold == 0.3  # type: ignore[call-arg]


def test_json_formatter_includes_extras_and_request_id() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "query.completed", None, None)
    record.status = "answered"
    token = request_id_var.set("req-123")
    try:
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert payload["event"] == "query.completed"
    assert payload["status"] == "answered"
    assert payload["request_id"] == "req-123"
    assert payload["level"] == "INFO"


def test_timer_records_stages() -> None:
    timer = Timer()
    with timer.stage("a"):
        pass
    assert set(timer.timings_ms) == {"a"} and timer.timings_ms["a"] >= 0


def test_environment_variables_override_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("RAG_TOP_K=3\nRAG_RETRIEVAL_MODE=lexical\n", encoding="utf-8")
    monkeypatch.setenv("RAG_TOP_K", "9")
    settings = Settings(_env_file=tmp_path / ".env")  # type: ignore[call-arg]
    assert settings.top_k == 9  # the environment wins
    assert settings.retrieval_mode == "lexical"  # .env fills what the environment doesn't set


# --- Configuration strategy (#4, #27): validation, safe defaults, environments ---------


def test_configuration_errors_name_variables_but_never_echo_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from production_rag.config import ConfigurationError, load_settings

    monkeypatch.setenv("RAG_API_KEY", "tiny-secret")  # too short: rejected
    monkeypatch.setenv("RAG_TOP_K", "0")
    with pytest.raises(ConfigurationError) as raised:
        load_settings()
    message = str(raised.value)
    assert "RAG_API_KEY: RAG_API_KEY must be at least 16 characters" in message
    assert "RAG_TOP_K: Input should be greater than or equal to 1" in message
    assert "tiny-secret" not in message
    assert raised.value.__suppress_context__  # the chained error would print the values


def test_cross_setting_rules_are_reported_too(monkeypatch: pytest.MonkeyPatch) -> None:
    from production_rag.config import ConfigurationError, load_settings

    monkeypatch.setenv("RAG_PARSE_TIMEOUT_S", "400")  # longer than the 300 s lease
    with pytest.raises(ConfigurationError, match="settings: RAG_PARSE_TIMEOUT_S must be shorter"):
        load_settings()


def test_the_cli_reports_bad_configuration_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from production_rag.cli import main

    monkeypatch.setenv("RAG_API_KEY", "tiny-secret")
    assert main(["db", "current"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("invalid configuration") and "RAG_API_KEY" in err
    assert "tiny-secret" not in err and "Traceback" not in err


def test_the_api_refuses_to_start_with_bad_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    from production_rag.api.app import app_factory

    monkeypatch.setenv("RAG_API_KEY", "tiny-secret")
    with pytest.raises(SystemExit) as raised:
        app_factory()
    assert "RAG_API_KEY" in str(raised.value.code)
    assert "tiny-secret" not in str(raised.value.code)


def test_defaults_are_a_safe_offline_development_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAG_PARSE_ISOLATION")  # the suite's own override (tests/conftest.py)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    # No credentials, no downloads, no paid calls: a fresh clone works as is.
    assert settings.embedding_provider == "hashing"
    assert settings.reranker == "none"
    assert settings.generator == "extractive"
    assert settings.anthropic_api_key is None
    # Security-relevant defaults that hold in every environment.
    assert settings.parse_isolation == "process"
    assert settings.max_upload_bytes == 25 * 1024 * 1024


def test_env_example_is_valid_and_holds_no_secrets() -> None:
    from tests.conftest import REPO_ROOT

    example = REPO_ROOT / ".env.example"
    settings = Settings(_env_file=example)  # type: ignore[call-arg]
    # It documents the recommended production-like setup...
    assert (settings.embedding_provider, settings.reranker, settings.generator) == (
        "fastembed",
        "fastembed",
        "anthropic",
    )
    # ...with every secret left empty: placeholders only.
    for line in example.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if not line.startswith("#") and any(k in name for k in ("KEY", "TOKEN", "SECRET")):
            assert value.strip() == "", f"{name} must be empty in .env.example"
    assert settings.api_key is None and settings.anthropic_api_key is None


def test_secret_files_are_ignored_by_git() -> None:
    from tests.conftest import REPO_ROOT

    rules = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for rule in (".env", ".env.*", "!.env.example", "*.pem", "*.key"):
        assert rule in rules, rule


def test_serving_publicly_without_an_api_key_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    from production_rag.cli import main

    monkeypatch.setattr(uvicorn, "run", lambda *_, **__: None)
    monkeypatch.setenv("RAG_DATABASE_URL", "sqlite://")
    assert main(["serve", "--host", "0.0.0.0"]) == 0
    assert "without RAG_API_KEY" in capsys.readouterr().err
    assert main(["serve"]) == 0  # loopback, the default: no warning
    assert "without RAG_API_KEY" not in capsys.readouterr().err
    get_settings.cache_clear()
    monkeypatch.setenv("RAG_API_KEY", "a-long-enough-api-key-0123")
    assert main(["serve", "--host", "0.0.0.0"]) == 0
    assert "without RAG_API_KEY" not in capsys.readouterr().err


def test_evaluation_never_touches_the_configured_database(tmp_path: Path) -> None:
    from production_rag.evaluation import run_evaluation
    from tests.conftest import CORPUS_DIR, DATASET

    production_db = tmp_path / "production.db"
    settings = Settings(_env_file=None, database_url=f"sqlite:///{production_db.as_posix()}")  # type: ignore[call-arg]
    report = run_evaluation(settings, corpus_dir=CORPUS_DIR, dataset_path=DATASET, split="dev")
    assert report["corpus"]["documents"] == 9  # it built its own index...
    assert not production_db.exists()  # ...in a temporary database, not this one
