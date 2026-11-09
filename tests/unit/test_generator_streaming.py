"""Streaming generation (FR-9), through the real Anthropic SDK and a mock HTTP transport.

The SDK parses real Messages API stream events and accumulates the message, so these tests
check this code against the SDK's own event handling, with no network and nothing billed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import anthropic
import httpx2
import pytest

from production_rag.config import Settings
from production_rag.generation import (
    AnswerComplete,
    CitationAdded,
    ExtractiveGenerator,
    GenerationError,
    GenerationEvent,
    TextDelta,
)
from production_rag.generation.anthropic_generator import (
    FALLBACK_BETA,
    REFUSAL_MESSAGE,
    AnthropicGenerator,
)
from tests.unit.test_generators import CHUNKS, _citation, _generator, _message

# --- Messages API stream events ---


def _start(model: str = "claude-opus-5-5") -> dict[str, Any]:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_stream",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 120, "output_tokens": 1},
        },
    }


def _block(index: int, block: dict[str, Any], *deltas: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"type": "content_block_start", "index": index, "content_block": block},
        *({"type": "content_block_delta", "index": index, "delta": d} for d in deltas),
        {"type": "content_block_stop", "index": index},
    ]


def _text(index: int, *pieces: str, cites: tuple[dict[str, Any], ...] = ()) -> list[dict[str, Any]]:
    deltas = [{"type": "citations_delta", "citation": c} for c in cites]
    deltas += [{"type": "text_delta", "text": piece} for piece in pieces]
    return _block(index, {"type": "text", "text": "", "citations": []}, *deltas)


def _thinking(index: int) -> list[dict[str, Any]]:
    # Adaptive thinking with the default display: an empty thinking block, then its signature.
    return _block(
        index,
        {"type": "thinking", "thinking": "", "signature": ""},
        {"type": "signature_delta", "signature": "sig"},
    )


def _end(stop_reason: str = "end_turn", **delta: Any) -> list[dict[str, Any]]:
    return [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None, **delta},
            "usage": {"output_tokens": 30},
        },
        {"type": "message_stop"},
    ]


def _sse(events: list[dict[str, Any]]) -> Iterator[bytes]:
    for event in events:
        yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


class _Body(httpx2.SyncByteStream):
    """A response body served piece by piece, which records whether the response closed it
    (what releases the connection to the API, so generation stops)."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield from _sse(self.events)

    def close(self) -> None:
        self.closed = True


def _streaming(
    respond: _Body | httpx2.Response, **settings: Any
) -> tuple[AnthropicGenerator, list[httpx2.Request]]:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if isinstance(respond, httpx2.Response):
            return respond
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=respond)

    client = anthropic.Anthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    return AnthropicGenerator(Settings(_env_file=None, **settings), client=client), requests  # type: ignore[call-arg]


def _events(generator: AnthropicGenerator) -> list[GenerationEvent]:
    return list(generator.stream("How long does the canary run?", CHUNKS))


CANARY = _citation(0, CHUNKS[0].text)
ROLLBACK = _citation(1, CHUNKS[1].text)
ANSWER = [
    *_thinking(0),
    *_text(1, "The canary runs ", "for 30 minutes. ", cites=(CANARY,)),
    *_text(2, "Then "),
    *_text(3, "roll back with tessera.", cites=(ROLLBACK, CANARY)),
]


def test_text_and_citations_stream_then_the_same_answer_as_generate() -> None:
    generator, requests = _streaming(_Body([_start(), *ANSWER, *_end()]))
    events = _events(generator)

    assert events[:-1] == [
        CitationAdded(0, events[0].citation),  # type: ignore[union-attr]
        TextDelta("The canary runs "),
        TextDelta("for 30 minutes. "),
        TextDelta("Then "),
        CitationAdded(1, events[4].citation),  # type: ignore[union-attr]
        TextDelta("roll back with tessera."),
    ]
    assert [e.citation.chunk.chunk_id for e in events if isinstance(e, CitationAdded)] == [1, 2]
    final = events[-1]
    assert isinstance(final, AnswerComplete)

    # The same content, answered in one response, gives exactly the same answer.
    blocks = [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "The canary runs for 30 minutes. ", "citations": [CANARY]},
        {"type": "text", "text": "Then ", "citations": None},
        {"type": "text", "text": "roll back with tessera.", "citations": [ROLLBACK, CANARY]},
    ]
    unstreamed, _ = _generator(_message(blocks))
    assert final.answer == unstreamed.generate("How long does the canary run?", CHUNKS)
    assert final.answer.status == "answered"
    assert [s.citations for s in final.answer.segments] == [[0], [], [1, 0]]

    # The request is the one generate() sends, plus streaming.
    (request,) = requests
    body = json.loads(request.content)
    assert body["stream"] is True
    expected = generator.build_request("How long does the canary run?", CHUNKS)
    assert {k: body[k] for k in ("model", "system", "messages", "fallbacks")} == {
        k: expected[k] for k in ("model", "system", "messages", "fallbacks")
    }
    assert FALLBACK_BETA in request.headers["anthropic-beta"]


def test_a_fallback_part_way_keeps_the_streamed_text() -> None:
    """With server-side fallbacks, the fallback model continues the same stream."""
    fallback = {
        "type": "fallback",
        "from": {"model": "claude-opus-5-5"},
        "to": {"model": "claude-opus-4-8"},
        "trigger": {"type": "refusal", "category": "cyber"},
    }
    events = _events(
        _streaming(
            _Body(
                [
                    _start(),
                    *_text(0, "The canary "),
                    *_block(1, fallback),
                    *_text(2, "runs for 30 minutes.", cites=(CANARY,)),
                    *_end(),
                ]
            )
        )[0]
    )
    streamed = "".join(e.text for e in events if isinstance(e, TextDelta))
    final = events[-1]
    assert isinstance(final, AnswerComplete)
    assert streamed == final.answer.text == "The canary runs for 30 minutes."
    assert final.answer.status == "answered"


def test_a_refusal_withdraws_the_streamed_text() -> None:
    refusal = _end("refusal", stop_details={"type": "refusal", "category": "cyber"})
    events = _events(_streaming(_Body([_start(), *_text(0, "Partial answer"), *refusal]))[0])
    assert events[0] == TextDelta("Partial answer")
    final = events[-1]
    assert isinstance(final, AnswerComplete)
    assert (final.answer.status, final.answer.text) == ("refused", REFUSAL_MESSAGE)
    assert final.answer.citations == []


def test_an_uncited_streamed_answer_is_not_reported_as_answered() -> None:
    events = _events(_streaming(_Body([_start(), *_text(0, "I don't know."), *_end()]))[0])
    final = events[-1]
    assert isinstance(final, AnswerComplete)
    assert (final.answer.status, final.answer.text) == ("insufficient_evidence", "I don't know.")


def test_citations_outside_the_retrieved_chunks_are_ignored() -> None:
    stray = _citation(7, "not retrieved")
    events = _events(
        _streaming(_Body([_start(), *_text(0, "x", cites=(stray, CANARY)), *_end()]))[0]
    )
    added = [e for e in events if isinstance(e, CitationAdded)]
    assert [(e.index, e.citation.chunk.chunk_id) for e in added] == [(0, 1)]


@pytest.mark.parametrize(
    ("response", "retryable", "message"),
    [
        (
            httpx2.Response(429, json={"type": "error", "error": {"type": "rate_limit_error"}}),
            True,
            "rate limit",
        ),
        (
            httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error"}}),
            False,
            "credentials",
        ),
        (
            httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error"}}),
            True,
            "HTTP 529",
        ),
    ],
)
def test_errors_before_the_stream_starts_are_classified(
    response: httpx2.Response, retryable: bool, message: str
) -> None:
    generator, _ = _streaming(response)
    with pytest.raises(GenerationError, match=message) as raised:
        _events(generator)
    assert raised.value.retryable is retryable


@pytest.mark.parametrize(
    ("error_type", "retryable"),
    [("overloaded_error", True), ("api_error", True), ("invalid_request_error", False)],
)
def test_an_error_event_during_the_stream_is_classified_by_its_type(
    error_type: str, retryable: bool
) -> None:
    class _ErrorBody(_Body):
        def __iter__(self) -> Iterator[bytes]:
            yield from _sse([_start(), *_text(0, "Part")[:2]])
            error = {"type": "error", "error": {"type": error_type, "message": "x"}}
            yield f"event: error\ndata: {json.dumps(error)}\n\n".encode()

    generator, _ = _streaming(_ErrorBody([]))
    stream = generator.stream("q", CHUNKS)
    assert next(stream) == TextDelta("Part")
    with pytest.raises(GenerationError, match=f"{error_type} during the stream") as raised:
        list(stream)
    assert raised.value.retryable is retryable


def test_closing_the_stream_early_closes_the_api_response() -> None:
    """A client that went away must not leave Claude generating, and billing, unread."""
    body = _Body([_start(), *ANSWER, *_end()])
    generator, _ = _streaming(body)
    stream = generator.stream("q", CHUNKS)
    next(stream)
    assert not body.closed
    stream.close()
    assert body.closed


def test_the_extractive_generator_streams_its_answer() -> None:
    generator = ExtractiveGenerator()
    question = "How long does the canary run?"
    events = list(generator.stream(question, CHUNKS))
    final = events[-1]
    assert isinstance(final, AnswerComplete)
    assert final.answer == generator.generate(question, CHUNKS)
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert "".join(texts) == final.answer.text
    cited = [e for e in events if isinstance(e, CitationAdded)]
    assert [e.index for e in cited] == list(range(len(final.answer.citations)))
    # Each citation is announced before the text it supports.
    assert isinstance(events[0], CitationAdded)
