"""Query orchestration: retrieve -> abstain or generate -> cited answer with timings."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from production_rag.config import RetrievalMode, Settings
from production_rag.generation import (
    INSUFFICIENT_EVIDENCE_MESSAGE,
    AnswerSegment,
    GeneratedAnswer,
    Generator,
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
                answer = GeneratedAnswer(
                    status="insufficient_evidence",
                    segments=[AnswerSegment(INSUFFICIENT_EVIDENCE_MESSAGE)],
                    citations=[],
                    model=self.generator.model_id,
                )
            else:
                with timer.stage("generation"):
                    answer = self.generator.generate(question, retrieval.chunks)

        logger.info(
            "query.completed",
            extra={
                "status": answer.status,
                "mode": retrieval.mode,
                "retrieved": len(retrieval.chunks),
                "citations": len(answer.citations),
                "best_vector_score": retrieval.best_vector_score,
                "best_rerank_score": retrieval.best_rerank_score,
                "abstained_before_generation": abstain,
                "model": answer.model,
                "timings_ms": timer.timings_ms,
            },
        )
        return QueryResult(answer, retrieval, abstain, timer.timings_ms)
