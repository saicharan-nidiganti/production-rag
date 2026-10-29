"""Parsing in a separate process (ADR-0007): same results, clean failures, no secrets."""

from __future__ import annotations

import json
import sys

import pytest
from pydantic import ValidationError

from production_rag.config import Settings
from production_rag.container import Container, build_container
from production_rag.parsing import ExtractionError, UnsupportedDocumentError, extract, isolation
from production_rag.parsing.isolation import ParseFailedError, ParseTimeoutError, parse
from tests.conftest import CORPUS_DIR
from tests.unit.test_extract import _pdf

RUNBOOK = (CORPUS_DIR / "deployment-runbook.md").read_bytes()


def _isolated(**overrides: object) -> Settings:
    return Settings(_env_file=None, parse_isolation="process", **overrides)  # type: ignore[call-arg]


def _fake_parser(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    """Replace the parser process with a tiny program, to exercise the failure paths."""
    monkeypatch.setattr(isolation, "_COMMAND", [sys.executable, "-c", code])


@pytest.mark.parametrize(
    ("name", "data"),
    [("deployment-runbook.md", RUNBOOK), ("two-pages.pdf", _pdf(["First page.", "Second page."]))],
)
def test_isolated_parsing_gives_the_same_result_as_inline(name: str, data: bytes) -> None:
    assert parse(name, data, _isolated()) == extract(name, data)


def test_expected_errors_keep_their_type_and_message() -> None:
    with pytest.raises(ExtractionError, match=r"^could not read PDF"):
        parse("broken.pdf", b"%PDF-1.7\nnot really a pdf", _isolated())
    with pytest.raises(UnsupportedDocumentError, match="not a PDF"):
        parse("fake.pdf", b"plain text", _isolated())


def test_a_parse_that_runs_too_long_is_killed() -> None:
    # Starting the parser process alone takes longer than this.
    with pytest.raises(ParseTimeoutError, match="RAG_PARSE_TIMEOUT_S"):
        parse("deployment-runbook.md", RUNBOOK, _isolated(parse_timeout_s=0.01))


def test_a_parser_that_dies_fails_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_parser(monkeypatch, "import sys; sys.exit(3)")
    with pytest.raises(ParseFailedError, match="exit code 3"):
        parse("deployment-runbook.md", RUNBOOK, _isolated())


def test_unreadable_parser_output_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_parser(monkeypatch, "print('not json')")
    with pytest.raises(ParseFailedError, match="exit code 0"):
        parse("deployment-runbook.md", RUNBOOK, _isolated())


def test_unexpected_parser_errors_report_only_their_type(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"error": {"type": "KeyError", "message": "secret document text"}})
    _fake_parser(monkeypatch, f"print({payload!r})")
    with pytest.raises(ParseFailedError) as raised:
        parse("deployment-runbook.md", RUNBOOK, _isolated())
    assert "KeyError" in str(raised.value) and "secret" not in str(raised.value)


@pytest.mark.skipif(sys.platform == "win32", reason="memory limits need POSIX rlimits")
def test_a_parse_over_the_memory_limit_fails_cleanly() -> None:
    huge = b"word " * (12 * 1024 * 1024)  # 60 MB of text: decoding needs well over 128 MB
    with pytest.raises(ParseFailedError):
        parse("huge.txt", huge, _isolated(parse_memory_mb=128))


def test_the_parser_gets_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-for-the-parser")
    monkeypatch.setenv("RAG_API_KEY", "service-key-not-for-the-parser")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-not-for-the-parser")
    monkeypatch.setenv("DB_PASSWORD", "not-for-the-parser")
    env = isolation.child_environment()
    assert not {"ANTHROPIC_API_KEY", "RAG_API_KEY", "GITHUB_TOKEN", "DB_PASSWORD"} & env.keys()
    assert "PATH" in env or "Path" in env  # still able to run Python


def test_the_parse_timeout_must_be_shorter_than_the_lease() -> None:
    with pytest.raises(ValidationError, match="RAG_PARSE_TIMEOUT_S"):
        Settings(_env_file=None, parse_timeout_s=300, ingest_lease_s=300)  # type: ignore[call-arg]


def test_ingestion_records_a_parse_timeout_on_the_document(migrated_settings: Settings) -> None:
    container: Container = build_container(
        migrated_settings.model_copy(update={"parse_isolation": "process", "parse_timeout_s": 0.01})
    )
    try:
        doc = container.ingestion.submit("deployment-runbook.md", RUNBOOK).document
        claim = container.ingestion.queue.claim_next()
        assert claim is not None
        assert container.ingestion.process(claim).status == "failed"
        failed = container.documents.get(doc.id)
        assert failed is not None and failed.error_code == "parse_timeout"
        assert "RAG_PARSE_TIMEOUT_S" in (failed.error_message or "")
    finally:
        container.close()


def test_ingestion_through_the_parser_process_reaches_ready(migrated_settings: Settings) -> None:
    container = build_container(migrated_settings.model_copy(update={"parse_isolation": "process"}))
    try:
        result = container.ingestion.ingest("deployment-runbook.md", RUNBOOK)
        assert result.document.status == "ready" and result.document.num_chunks >= 4
    finally:
        container.close()
