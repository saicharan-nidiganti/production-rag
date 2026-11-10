"""Query orchestration: retrieve -> abstain or generate -> cited answer with timings."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass

from production_rag import metrics
from production_rag.config import RetrievalMode, Settings
from production_rag.generation import (
    INSUFFICIENT_EVIDENCE_MESSAGE,
    AnswerComplete,
    AnswerSegment,
    CitationAdded,
    GeneratedAnswer,
    Generator,
    TextDelta,
)
from production_rag.observability import Timer
from production_rag.retrieval import Retriever, SearchResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueryResult:
    answer: GeneratedAnswer
    retrieval: SearchResult
    abstained_before_generation: bool
    timings_ms: dict[str, float]


@dataclass(frozen=True)
class RetrievalDone:
    """The first event of a streamed answer: what was retrieved, and whether the service
    abstains without calling the generator."""

    retrieval: SearchResult
    abstained_before_generation: bool


@dataclass(frozen=True)
class QueryDone:
    """The last event of a streamed answer: the result ``answer`` would have returned."""

    result: QueryResult


QueryEvent = RetrievalDone | TextDelta | CitationAdded | QueryDone


class RagService:
    def __init__(self, retriever: Retriever, generator: Generator, settings: Settings) -> None:
        self.retriever = retriever
        self.generator = generator
        self._settings = settings

    def search(
        self, query: str, *, top_k: int | None = None, mode: RetrievalMode | None = None
    ) -> SearchResult:
        return self.retriever.search(
            query,
            top_k=top_k or self._settings.top_k,
            mode=mode or self._settings.retrieval_mode,
        )

    def should_abstain(self, result: SearchResult) -> bool:
        """Abstain without calling the generator when retrieval found no usable evidence.

        Each threshold applies only when its score exists: cosine when vector search ran,
        the rerank score when a reranker ran. Both are calibrated on the dev split of the
        evaluation set (docs/evaluation.md).
        """
        if not result.chunks:
            return True
        cosine = result.best_vector_score
        if cosine is not None and cosine < self._settings.abstain_threshold:
            return True
        rerank_threshold = self._settings.rerank_abstain_threshold
        rerank = result.best_rerank_score
        return rerank is not None and rerank_threshold is not None and rerank < rerank_threshold

    def answer(
        self, question: str, *, top_k: int | None = None, mode: RetrievalMode | None = None
    ) -> QueryResult:
        timer = Timer()
        with timer.stage("total"):
            with timer.stage("retrieval"):
                retrieval = self.search(question, top_k=top_k, mode=mode)
            if retrieval.rerank_ms is not None:
                timer.timings_ms["rerank"] = retrieval.rerank_ms  # included in "retrieval"
            abstain = self.should_abstain(retrieval)
            if abstain:
                answer = self._abstention()
            else:
                with timer.stage("generation"):
                    answer = self.generator.generate(question, retrieval.chunks)

        result = QueryResult(answer, retrieval, abstain, timer.timings_ms)
        self._log_completed(result, streamed=False)
        return result

    def stream_answer(
        self, question: str, *, top_k: int | None = None, mode: RetrievalMode | None = None
    ) -> Iterator[QueryEvent]:
        """``answer``, as it happens (FR-9).

        Events, in order: one ``RetrievalDone``; then, unless the service abstains, the
        generator's ``TextDelta`` and ``CitationAdded`` events as they arrive; then one
        ``QueryDone`` with the same result ``answer`` returns. Timings add ``first_token``,
        from the start of the request to the first text. ``generation`` includes the time the
        caller spent consuming events. Closing the iterator early stops the generator.
        """
        start = time.perf_counter()
        timer = Timer()
        with timer.stage("retrieval"):
            retrieval = self.search(question, top_k=top_k, mode=mode)
        if retrieval.rerank_ms is not None:
            timer.timings_ms["rerank"] = retrieval.rerank_ms  # included in "retrieval"
        abstain = self.should_abstain(retrieval)
        yield RetrievalDone(retrieval, abstain)

        answer: GeneratedAnswer | None = self._abstention() if abstain else None
        if answer is None:
            with timer.stage("generation"):
                for event in self.generator.stream(question, retrieval.chunks):
                    if isinstance(event, AnswerComplete):
                        answer = event.answer
                        continue
                    if isinstance(event, TextDelta) and "first_token" not in timer.timings_ms:
                        timer.timings_ms["first_token"] = _elapsed_ms(start)
                    yield event
            if answer is None:
                raise RuntimeError("the generator's stream ended without an answer")
        timer.timings_ms["total"] = _elapsed_ms(start)

        result = QueryResult(answer, retrieval, abstain, timer.timings_ms)
        self._log_completed(result, streamed=True)
        yield QueryDone(result)

    def _abstention(self) -> GeneratedAnswer:
        return GeneratedAnswer(
            status="insufficient_evidence",
            segments=[AnswerSegment(INSUFFICIENT_EVIDENCE_MESSAGE)],
            citations=[],
            model=self.generator.model_id,
        )

    def _log_completed(self, result: QueryResult, *, streamed: bool) -> None:
        answer, retrieval = result.answer, result.retrieval
        logger.info(
            "query.completed",
            extra={
                "status": answer.status,
                "mode": retrieval.mode,
                "retrieved": len(retrieval.chunks),
                "citations": len(answer.citations),
                "best_vector_score": retrieval.best_vector_score,
                "best_rerank_score": retrieval.best_rerank_score,
                "abstained_before_generation": result.abstained_before_generation,
                "model": answer.model,
                "streamed": streamed,
                "timings_ms": result.timings_ms,
            },
        )
        metrics.observe_query(
            status=answer.status,
            streamed=streamed,
            abstained=result.abstained_before_generation,
            timings_ms=result.timings_ms,
        )


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)
