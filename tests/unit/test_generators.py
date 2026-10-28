from __future__ import annotations

from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types.beta import BetaMessage

from production_rag.config import Settings
from production_rag.generation import ExtractiveGenerator, GenerationError
from production_rag.generation.anthropic_generator import (
    FALLBACK_BETA,
    SYSTEM_PROMPT,
    AnthropicGenerator,
    chunk_title,
)
from production_rag.generation.extractive import sentences
from tests.conftest import make_chunk

CHUNKS = [
    make_chunk(1, "The canary receives 5% of production traffic for 30 minutes."),
    make_chunk(
        2, "Run tessera rollout undo to roll back.", heading="Deployment Runbook > Rollback"
    ),
]


def _citation(index: int, text: str) -> dict[str, Any]:
    return {
        "type": "search_result_location",
        "cited_text": text,
        "source": f"src-{index}",
        "title": "t",
        "search_result_index": index,
        "start_block_index": 0,
        "end_block_index": 1,
    }


def _message(
    content: list[dict[str, Any]], stop_reason: str = "end_turn", **extra: Any
) -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "content": content,
            "usage": {"input_tokens": 120, "output_tokens": 30},
            **extra,
        }
    )


class FakeMessages:
    def __init__(self, result: BetaMessage | Exception) -> None:
        self.result = result
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> BetaMessage:
        self.requests.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeClient:
    def __init__(self, result: BetaMessage | Exception) -> None:
        self.messages = FakeMessages(result)
        self.beta = self


def _generator(
    result: BetaMessage | Exception, **settings: Any
) -> tuple[AnthropicGenerator, FakeMessages]:
    client = FakeClient(result)
    gen = AnthropicGenerator(Settings(_env_file=None, **settings), client=client)  # type: ignore[arg-type,call-arg]
    return gen, client.messages


def test_request_uses_search_results_with_citations_and_fallbacks() -> None:
    gen, messages = _generator(_message([{"type": "text", "text": "ok"}]))
    gen.generate("How long is the canary?", CHUNKS)
    (request,) = messages.requests
    assert request["model"] == "claude-opus-5-5"
    assert request["system"] == SYSTEM_PROMPT
    assert request["output_config"] == {"effort": "medium"}
    assert request["betas"] == [FALLBACK_BETA]
    assert request["fallbacks"] == "default"
    assert "thinking" not in request and "temperature" not in request
    content = request["messages"][0]["content"]
    assert [b["type"] for b in content] == ["search_result", "search_result", "text"]
    assert content[0]["citations"] == {"enabled": True}
    assert content[0]["content"] == [{"type": "text", "text": CHUNKS[0].text}]
    assert content[1]["title"] == "Deployment Runbook — Deployment Runbook > Rollback"
    assert content[2]["text"] == "How long is the canary?"


def test_fallbacks_can_be_disabled() -> None:
    gen, messages = _generator(
        _message([{"type": "text", "text": "ok"}]), anthropic_fallbacks=False
    )
    gen.generate("q", CHUNKS)
    assert "betas" not in messages.requests[0] and "fallbacks" not in messages.requests[0]


def test_citations_map_back_to_retrieved_chunks() -> None:
    response = _message(
        [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {
                "type": "text",
                "text": "The canary runs for 30 minutes",
                "citations": [_citation(0, CHUNKS[0].text)],
            },
            {"type": "text", "text": ", and "},
            {
                "type": "text",
                "text": "you roll back with tessera rollout undo.",
                "citations": [
                    _citation(1, CHUNKS[1].text),
                    _citation(0, CHUNKS[0].text),
                    _citation(9, "bogus"),
                ],
            },
        ]
    )
    gen, _ = _generator(response)
    answer = gen.generate("q", CHUNKS)
    assert answer.status == "answered"
    assert (
        answer.text
        == "The canary runs for 30 minutes, and you roll back with tessera rollout undo."
    )
    assert [c.chunk.chunk_id for c in answer.citations] == [
        1,
        2,
    ]  # de-duplicated, bogus index dropped
    assert [s.citations for s in answer.segments] == [[0], [], [1, 0]]
    assert answer.usage == {"input_tokens": 120, "output_tokens": 30}


def test_uncited_answer_is_not_reported_as_answered() -> None:
    gen, _ = _generator(_message([{"type": "text", "text": "The documents don't say."}]))
    answer = gen.generate("q", CHUNKS)
    assert answer.status == "insufficient_evidence"
    assert answer.text == "The documents don't say."


def test_refusal_is_surfaced() -> None:
    response = _message(
        [],
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": None},
    )
    gen, _ = _generator(response)
    answer = gen.generate("q", CHUNKS)
    assert answer.status == "refused"
    assert answer.citations == []
    assert answer.text == "The model declined to answer this request."


def test_bad_request_message_does_not_echo_sdk_details() -> None:
    error = anthropic.BadRequestError(
        "messages.0: secret request detail",
        response=httpx2.Response(400, request=_REQUEST),
        body=None,
    )
    gen, _ = _generator(error)
    with pytest.raises(GenerationError) as info:
        gen.generate("q", CHUNKS)
    assert str(info.value) == "Claude API rejected the request (HTTP 400)"


_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (
            anthropic.RateLimitError(
                "rl", response=httpx2.Response(429, request=_REQUEST), body=None
            ),
            True,
        ),
        (
            anthropic.InternalServerError(
                "x", response=httpx2.Response(529, request=_REQUEST), body=None
            ),
            True,
        ),
        (anthropic.APIConnectionError(request=_REQUEST), True),
        (
            anthropic.AuthenticationError(
                "x", response=httpx2.Response(401, request=_REQUEST), body=None
            ),
            False,
        ),
        (
            anthropic.BadRequestError(
                "x", response=httpx2.Response(400, request=_REQUEST), body=None
            ),
            False,
        ),
        (
            anthropic.NotFoundError(
                "x", response=httpx2.Response(404, request=_REQUEST), body=None
            ),
            False,
        ),
    ],
)
def test_sdk_errors_are_classified(error: Exception, retryable: bool) -> None:
    gen, _ = _generator(error)
    with pytest.raises(GenerationError) as info:
        gen.generate("q", CHUNKS)
    assert info.value.retryable is retryable


def test_missing_credentials_become_a_configuration_error() -> None:
    gen, _ = _generator(
        TypeError('"Could not resolve authentication method. Expected one of api_key"')
    )
    with pytest.raises(GenerationError, match="no Claude API credentials") as info:
        gen.generate("q", CHUNKS)
    assert info.value.retryable is False


def test_unrelated_type_errors_still_propagate() -> None:
    gen, _ = _generator(TypeError("unexpected keyword argument 'foo'"))
    with pytest.raises(TypeError, match="foo"):
        gen.generate("q", CHUNKS)


def test_chunk_title_includes_page() -> None:
    assert chunk_title(make_chunk(heading=None, page=3)) == "Deployment Runbook (page 3)"


def test_extractive_generator_quotes_and_cites_relevant_sentences() -> None:
    answer = ExtractiveGenerator().generate("How long does the canary run?", CHUNKS)
    assert answer.status == "answered"
    assert "30 minutes" in answer.text
    assert answer.citations[0].chunk.chunk_id == 1
    assert all(seg.citations for seg in answer.segments)


def test_sentences_join_wrapped_lines_and_drop_headings() -> None:
    # Regression: a hard line wrap used to end the sentence, so "for\n30 minutes." was cut.
    text = (
        "## Canary releases\n\n"
        "The canary gets 5% of traffic for\n30 minutes. Then it grows!\n\nNext para"
    )
    assert sentences(text) == [
        "The canary gets 5% of traffic for 30 minutes.",
        "Then it grows!",
        "Next para",
    ]


def test_extractive_generator_abstains_without_overlap() -> None:
    answer = ExtractiveGenerator().generate("sourdough recipe", CHUNKS)
    assert answer.status == "insufficient_evidence"
    assert answer.citations == []
