"""Generator interface and the answer types shared by all generators."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from production_rag.retrieval import RetrievedChunk

AnswerStatus = Literal["answered", "insufficient_evidence", "refused"]

INSUFFICIENT_EVIDENCE_MESSAGE = (
    "I couldn't find enough information in the indexed documents to answer this question."
)


@dataclass(frozen=True)
class Citation:
    chunk: RetrievedChunk
    cited_text: str


@dataclass(frozen=True)
class AnswerSegment:
    """A span of answer text and the citations (indexes into ``GeneratedAnswer.citations``)
    that support it. Uncited connective text has an empty list."""

    text: str
    citations: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class GeneratedAnswer:
    status: AnswerStatus
    segments: list[AnswerSegment]
    citations: list[Citation]
    model: str
    stop_reason: str | None = None
    usage: dict[str, int] | None = None

    @property
    def text(self) -> str:
        return "".join(segment.text for segment in self.segments)


@dataclass(frozen=True)
class TextDelta:
    """More answer text, as it is generated."""

    text: str


@dataclass(frozen=True)
class CitationAdded:
    """A citation seen for the first time while streaming. ``index`` is its position in the
    final answer's ``citations``, unless the answer ends up refused."""

    index: int
    citation: Citation


@dataclass(frozen=True)
class AnswerComplete:
    """The complete answer, always the last event of a stream and the authoritative one: a
    refused answer replaces any text streamed before it."""

    answer: GeneratedAnswer


GenerationEvent = TextDelta | CitationAdded | AnswerComplete


class GenerationError(RuntimeError):
    """The generation backend failed. ``retryable`` separates transient faults (rate limits,
    overload, network) from configuration problems (bad credentials, unknown model)."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class Generator(Protocol):
    @property
    def model_id(self) -> str: ...

    def generate(self, question: str, chunks: Sequence[RetrievedChunk]) -> GeneratedAnswer: ...

    def stream(self, question: str, chunks: Sequence[RetrievedChunk]) -> Iterator[GenerationEvent]:
        """``generate``, as it happens: text and citations as they are produced, then one
        ``AnswerComplete`` with the answer ``generate`` would have returned."""
        ...


def replay(answer: GeneratedAnswer) -> Iterator[GenerationEvent]:
    """Stream an answer that is already complete: each citation before the first text it
    supports, the text segment by segment, then the answer itself."""
    announced: set[int] = set()
    for segment in answer.segments:
        for index in segment.citations:
            if index not in announced:
                announced.add(index)
                yield CitationAdded(index, answer.citations[index])
        yield TextDelta(segment.text)
    yield AnswerComplete(answer)


class CitationCollector:
    """Builds the segment list, de-duplicating citations that point at the same chunk span."""

    def __init__(self) -> None:
        self.segments: list[AnswerSegment] = []
        self.citations: list[Citation] = []
        self._index: dict[tuple[int, str], int] = {}

    def add(self, text: str, cited: Sequence[Citation] = ()) -> None:
        refs: list[int] = []
        for citation in cited:
            key = (citation.chunk.chunk_id, citation.cited_text)
            if key not in self._index:
                self._index[key] = len(self.citations)
                self.citations.append(citation)
            if self._index[key] not in refs:
                refs.append(self._index[key])
        self.segments.append(AnswerSegment(text, refs))
