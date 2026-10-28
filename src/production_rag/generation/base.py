"""Generator interface and the answer types shared by all generators."""

from __future__ import annotations

from collections.abc import Sequence
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
