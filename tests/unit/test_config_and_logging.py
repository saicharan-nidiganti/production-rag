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
