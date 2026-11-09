"""Server-Sent Events for ``POST /v1/query/stream`` (FR-9).

A stream is a sequence of events, each ``event: <name>`` and one line of ``data: <JSON>``:

- ``retrieval``, once: the retrieved passages, and whether the service abstains.
- ``text``, any number: more answer text, to append.
- ``citation``, any number: a citation seen for the first time, with its index in ``done``.
- ``done``, once, last: the complete response, exactly what ``POST /v1/query`` returns. It is
  authoritative: when its ``status`` is ``refused``, the text streamed before it must be
  discarded and replaced by ``done.answer``.
- ``error``, instead of ``done``: generation failed after the response had started, so the
  failure can't be an HTTP status. Same envelope as error responses.

Retrieval runs before the response starts, so its errors are ordinary HTTP errors.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import anyio
from pydantic import BaseModel

from production_rag.api.schemas import (
    CitationOut,
    ErrorBody,
    ErrorResponse,
    QueryResponse,
    SearchHit,
    StreamCitation,
    StreamRetrieval,
    StreamText,
)
from production_rag.generation import CitationAdded, GenerationError, TextDelta
from production_rag.rag import QueryDone, QueryEvent, RetrievalDone

logger = logging.getLogger(__name__)

MEDIA_TYPE = "text/event-stream"
# No caching, and no buffering by proxies such as nginx, which would hold events back.
HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def encode(name: str, data: BaseModel, **dump: Any) -> str:
    # JSON never contains a raw newline, so one data line always holds the whole event.
    return f"event: {name}\ndata: {data.model_dump_json(**dump)}\n\n"


def _encode_event(event: QueryEvent) -> str:
    if isinstance(event, RetrievalDone):
        return encode(
            "retrieval",
            StreamRetrieval(
                retrieved=[SearchHit.from_chunk(c) for c in event.retrieval.chunks],
                abstained_before_generation=event.abstained_before_generation,
            ),
        )
    if isinstance(event, TextDelta):
        return encode("text", StreamText(text=event.text))
    if isinstance(event, CitationAdded):
        citation = CitationOut.from_citation(event.citation)
        return encode("citation", StreamCitation(index=event.index, citation=citation))
    return encode("done", QueryResponse.from_result(event.result))


def _error(code: str, message: str, request_id: str | None) -> str:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, request_id=request_id))
    return encode("error", body, exclude={"error": {"details"}})


def sse_events(
    first: QueryEvent, rest: Iterator[QueryEvent], request_id: str | None
) -> Iterator[str]:
    """The stream's events, with a failure after the first one turned into an ``error``.

    Closing this iterator closes ``rest``, which stops the generation behind it.
    """
    try:
        for event in itertools.chain([first], rest):
            yield _encode_event(event)
            if isinstance(event, QueryDone):
                return
    except GenerationError as exc:
        logger.error(
            "generation.failed", extra={"retryable": exc.retryable, "streamed": True}, exc_info=exc
        )
        code = "generation_unavailable" if exc.retryable else "generation_failed"
        yield _error(code, str(exc), request_id)
    except Exception as exc:  # the response has started: report it in the stream
        logger.error("request.unhandled_error", extra={"streamed": True}, exc_info=exc)
        yield _error("internal_error", "internal server error", request_id)
    finally:
        close = getattr(rest, "close", None)
        if close is not None:
            close()


def _next(events: Iterator[str]) -> str | None:
    return next(events, None)


async def iterate_closing(events: Iterator[str]) -> AsyncIterator[str]:
    """Like Starlette's ``iterate_in_threadpool``, but closes ``events`` when the client goes
    away, which ends the generation (and the stream to the Claude API) instead of letting it
    run unread. A read in progress finishes first, so the iterator is never closed while it
    is still running."""
    try:
        while (chunk := await anyio.to_thread.run_sync(_next, events)) is not None:
            yield chunk
    finally:
        close = getattr(events, "close", None)
        if close is not None:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(close)
