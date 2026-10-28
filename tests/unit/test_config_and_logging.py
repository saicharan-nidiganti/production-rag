import json
import logging

import pytest
from pydantic import ValidationError

from production_rag.config import Settings, get_settings
from production_rag.observability import JsonFormatter, Timer, request_id_var


def test_settings_read_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_TOP_K", "7")
    monkeypatch.setenv("RAG_GENERATOR", "anthropic")
    monkeypatch.setenv("RAG_API_KEY", "s3cret")
    settings = get_settings()
    assert settings.top_k == 7
    assert settings.generator == "anthropic"
    assert settings.api_key is not None and settings.api_key.get_secret_value() == "s3cret"
    assert "s3cret" not in repr(settings)


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
