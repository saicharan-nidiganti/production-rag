"""Retrieval and answer metrics. All functions are pure, so they're simple to test."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from production_rag.evaluation.dataset import RelevantSource
from production_rag.generation import GeneratedAnswer
from production_rag.retrieval import RetrievedChunk


@dataclass(frozen=True)
class RetrievalScores:
    hit: float
    recall: float
    reciprocal_rank: float
    precision: float
    first_relevant_rank: int | None


def score_retrieval(
    retrieved: Sequence[RetrievedChunk], relevant: Sequence[RelevantSource]
) -> RetrievalScores:
    flags = [any(target.matches(chunk) for target in relevant) for chunk in retrieved]
    covered = sum(1 for target in relevant if any(target.matches(c) for c in retrieved))
    first = next((i + 1 for i, flag in enumerate(flags) if flag), None)
    return RetrievalScores(
        hit=1.0 if first is not None else 0.0,
        recall=covered / len(relevant) if relevant else 0.0,
        reciprocal_rank=1.0 / first if first is not None else 0.0,
        precision=sum(flags) / len(flags) if flags else 0.0,
        first_relevant_rank=first,
    )


def citation_precision(answer: GeneratedAnswer, relevant: Sequence[RelevantSource]) -> float | None:
    """Fraction of citations that point at a relevant source; None when nothing was cited."""
    if not answer.citations:
        return None
    good = sum(1 for c in answer.citations if any(t.matches(c.chunk) for t in relevant))
    return good / len(answer.citations)


def fact_recall(answer_text: str, must_include: Sequence[str]) -> float | None:
    """Fraction of expected key facts present in the answer (case-insensitive substring).

    A cheap, deterministic proxy for answer correctness; it can't credit paraphrases.
    """
    if not must_include:
        return None
    text = answer_text.lower()
    return sum(1 for fact in must_include if fact.lower() in text) / len(must_include)


def mean(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile, ``q`` in [0, 100]."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]
