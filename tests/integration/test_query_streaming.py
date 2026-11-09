"""Streamed answers (FR-9): the service's event stream, the SSE endpoint and the CLI."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from production_rag.api.streaming import iterate_closing, sse_events
from production_rag.cli import main
from production_rag.container import Container
from production_rag.generation import (
    AnswerComplete,
    GeneratedAnswer,
    GenerationError,
    GenerationEvent,
    TextDelta,
)
from production_rag.rag import QueryDone, RagService, RetrievalDone
from production_rag.retrieval import RetrievedChunk
from tests.conftest import CORPUS_DIR
from tests.integration.test_api import _upload_and_wait

QUESTION = "How long does the canary run?"


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, db_url: str) -> str:
    monkeypatch.setenv("RAG_DATABASE_URL", db_url)
    monkeypatch.setenv("RAG_LOG_LEVEL", "WARNING")
    return db_url


def _parse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Server-Sent Events as (event name, JSON data) pairs."""
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


class _ScriptedGenerator:
    """A generator whose stream is a fixed script, and which records when it is closed."""

    model_id = "scripted"

    def __init__(self, *script: GenerationEvent | Exception) -> None:
        self.script = script
        self.closed = False

    def generate(self, question: str, chunks: Sequence[RetrievedChunk]) -> GeneratedAnswer:
        raise AssertionError("the streaming path must not call generate()")

    def stream(self, question: str, chunks: Sequence[RetrievedChunk]) -> Iterator[GenerationEvent]:
        try:
            for step in self.script:
                if isinstance(step, Exception):
                    raise step
                yield step
        finally:
            self.closed = True


# --- The service's event stream ---


def test_the_event_stream_ends_with_what_answer_returns(corpus_container: Container) -> None:
    rag = corpus_container.rag
    events = list(rag.stream_answer(QUESTION))
    assert isinstance(events[0], RetrievalDone) and not events[0].abstained_before_generation
    assert isinstance(events[-1], QueryDone)
    result = events[-1].result
    assert result.answer == rag.answer(QUESTION).answer
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == result.answer.text
    assert {"retrieval", "first_token", "generation", "total"} <= result.timings_ms.keys()
    assert result.timings_ms["first_token"] <= result.timings_ms["total"]


def test_an_abstention_streams_no_text(container: Container) -> None:
    events = list(container.rag.stream_answer(QUESTION))  # nothing is indexed
    assert [type(e) for e in events] == [RetrievalDone, QueryDone]
    assert events[0].abstained_before_generation  # type: ignore[union-attr]
    done = events[-1]
    assert isinstance(done, QueryDone) and done.result.answer.status == "insufficient_evidence"
    assert "first_token" not in done.result.timings_ms


def test_closing_the_event_stream_stops_the_generator(corpus_container: Container) -> None:
    generator = _ScriptedGenerator(TextDelta("a"), TextDelta("b"))
    rag = RagService(corpus_container.rag.retriever, generator, corpus_container.settings)
    events = rag.stream_answer(QUESTION)
    assert isinstance(next(events), RetrievalDone)
    assert next(events) == TextDelta("a")
    events.close()
    assert generator.closed


def test_a_generator_stream_without_an_answer_is_an_error(corpus_container: Container) -> None:
    generator = _ScriptedGenerator(TextDelta("a"))
    rag = RagService(corpus_container.rag.retriever, generator, corpus_container.settings)
    with pytest.raises(RuntimeError, match="without an answer"):
        list(rag.stream_answer(QUESTION))


# --- POST /v1/query/stream ---


def test_the_stream_sends_retrieval_text_citations_then_the_query_response(
    client: TestClient,
) -> None:
    assert _upload_and_wait(client)["status"] == "ready"
    response = client.post("/v1/query/stream", json={"question": QUESTION})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"

    events = _parse(response.text)
    names = [name for name, _ in events]
    assert names[0] == "retrieval" and names[-1] == "done"
    assert set(names[1:-1]) == {"text", "citation"}
    retrieval, done = events[0][1], events[-1][1]
    assert retrieval["abstained_before_generation"] is False
    assert retrieval["retrieved"] == done["retrieved"]
    assert "".join(data["text"] for name, data in events if name == "text") == done["answer"]
    citations = [data for name, data in events if name == "citation"]
    assert [c["index"] for c in citations] == list(range(len(done["citations"])))
    assert all(done["citations"][c["index"]] == c["citation"] for c in citations)

    # `done` is the /v1/query response for the same question (timings aside).
    plain = client.post("/v1/query", json={"question": QUESTION}).json()
    assert {k: v for k, v in done.items() if k != "timings_ms"} == {
        k: v for k, v in plain.items() if k != "timings_ms"
    }
    assert "first_token" in done["timings_ms"]


def test_an_empty_index_streams_an_abstention(client: TestClient) -> None:
    events = _parse(client.post("/v1/query/stream", json={"question": QUESTION}).text)
    assert [name for name, _ in events] == ["retrieval", "done"]
    assert events[0][1]["abstained_before_generation"] is True
    assert events[1][1]["status"] == "insufficient_evidence"


@pytest.mark.parametrize(
    ("retryable", "code"), [(True, "generation_unavailable"), (False, "generation_failed")]
)
def test_a_generation_failure_after_the_start_is_an_error_event(
    container: Container,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    retryable: bool,
    code: str,
) -> None:
    assert _upload_and_wait(client)["status"] == "ready"
    failure = GenerationError("Claude API error (overloaded_error)", retryable=retryable)
    monkeypatch.setattr(container.rag, "generator", _ScriptedGenerator(TextDelta("Part"), failure))
    response = client.post("/v1/query/stream", json={"question": QUESTION})
    assert response.status_code == 200  # the stream had started
    events = _parse(response.text)
    assert [name for name, _ in events] == ["retrieval", "text", "error"]
    error = events[-1][1]["error"]
    assert (error["code"], error["message"]) == (code, "Claude API error (overloaded_error)")
    assert error["request_id"] == response.headers["X-Request-ID"]


def test_an_unexpected_failure_after_the_start_reveals_nothing(
    container: Container, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _upload_and_wait(client)["status"] == "ready"
    boom = RuntimeError("internal detail that must not leak")
    monkeypatch.setattr(container.rag, "generator", _ScriptedGenerator(boom))
    response = client.post("/v1/query/stream", json={"question": QUESTION})
    events = _parse(response.text)
    assert events[-1] == (
        "error",
        {
            "error": {
                "code": "internal_error",
                "message": "internal server error",
                "request_id": response.headers["X-Request-ID"],
            }
        },
    )
    assert "must not leak" not in response.text


def test_retrieval_failures_are_ordinary_http_errors(
    container: Container, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError("index unavailable, internal detail")

    monkeypatch.setattr(container.rag, "search", broken)
    response = client.post("/v1/query/stream", json={"question": QUESTION})
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "internal_error"
    assert "internal detail" not in response.text


def test_validation_and_authentication_apply_before_the_stream(
    container: Container, client: TestClient
) -> None:
    invalid = client.post("/v1/query/stream", json={"question": ""})
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "validation_error"
    container.settings.api_key = SecretStr("s" * 24)  # long enough; obviously fake
    assert client.post("/v1/query/stream", json={"question": QUESTION}).status_code == 401


def test_the_stream_shares_the_question_rate_limit(
    container: Container, client: TestClient
) -> None:
    container.settings.rate_limit_query_per_minute = 1
    assert client.post("/v1/query", json={"question": QUESTION}).status_code == 200
    limited = client.post("/v1/query/stream", json={"question": QUESTION})
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "rate_limited"


# --- Disconnects ---


def test_closing_the_sse_events_closes_the_answer_stream() -> None:
    closed: list[bool] = []

    def rest() -> Iterator[TextDelta]:
        try:
            yield TextDelta("b")
            yield TextDelta("c")
        finally:
            closed.append(True)

    events = sse_events(TextDelta("a"), rest(), None)
    assert next(events).startswith("event: text")
    assert next(events).startswith("event: text")
    events.close()
    assert closed == [True]


def test_a_client_that_goes_away_closes_the_events() -> None:
    closed: list[bool] = []

    def events() -> Iterator[str]:
        try:
            yield from ("a", "b", "c")
        finally:
            closed.append(True)

    source = events()  # held here, so only an explicit close (not garbage collection) closes it

    async def read_one_then_leave() -> None:
        stream = iterate_closing(source)
        assert await stream.__anext__() == "a"
        await stream.aclose()  # what Starlette does when the client disconnects
        assert closed == [True]

    anyio.run(read_one_then_leave)


# --- rag ask --stream ---


def test_cli_ask_streams_the_answer(cli_env: str, capsys: pytest.CaptureFixture[str]) -> None:
    main(["db", "upgrade"])
    main(["ingest", str(CORPUS_DIR / "deployment-runbook.md")])
    capsys.readouterr()
    assert main(["ask", "--stream", QUESTION]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Production rollouts start with a canary.")
    assert "[answered | extractive-v1]" in out
    assert "[1] deployment-runbook.md - Deployment Runbook > Canary releases" in out


def test_cli_ask_streaming_reports_a_withdrawn_answer(
    cli_env: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    refused = GeneratedAnswer(status="refused", segments=[], citations=[], model="scripted")
    generator = _ScriptedGenerator(TextDelta("Partial"), AnswerComplete(refused))
    monkeypatch.setattr("production_rag.rag.RagService.should_abstain", lambda *_: False)
    monkeypatch.setattr(
        "production_rag.generation.ExtractiveGenerator.stream",
        lambda self, question, chunks: generator.stream(question, chunks),
    )
    main(["db", "upgrade"])
    main(["ingest", str(CORPUS_DIR / "deployment-runbook.md")])
    capsys.readouterr()
    assert main(["ask", "--stream", QUESTION]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Partial")
    assert "[refused | scripted]\nThe text above is withdrawn." in out
