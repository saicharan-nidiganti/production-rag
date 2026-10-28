"""Offline extractive generator: quotes the most relevant retrieved sentences, each cited.

It makes no network calls and is fully deterministic, so the whole pipeline runs (and is
tested) without credentials. It is a baseline, not a substitute for an LLM: it cannot
synthesise, paraphrase or reason across passages.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from production_rag.generation.base import (
    INSUFFICIENT_EVIDENCE_MESSAGE,
    AnswerSegment,
    Citation,
    CitationCollector,
    GeneratedAnswer,
)
from production_rag.retrieval import RetrievedChunk
from production_rag.text import content_tokens

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_HEADING_LINE = re.compile(r"^\s*#{1,6}\s")


def sentences(text: str) -> list[str]:
    """Split into sentences. Single newlines are treated as wrapping inside a paragraph,
    and Markdown heading lines are dropped."""
    result: list[str] = []
    for paragraph in _PARAGRAPH_BREAK.split(text):
        lines = [line for line in paragraph.splitlines() if not _HEADING_LINE.match(line)]
        flat = " ".join(" ".join(lines).split())
        result.extend(s for s in _SENTENCE_BREAK.split(flat) if s)
    return result


class ExtractiveGenerator:
    def __init__(self, max_sentences: int = 3) -> None:
        self._max_sentences = max_sentences

    @property
    def model_id(self) -> str:
        return "extractive-v1"

    def generate(self, question: str, chunks: Sequence[RetrievedChunk]) -> GeneratedAnswer:
        terms = set(content_tokens(question))
        candidates: list[tuple[float, int, int, str]] = []  # (-score, chunk rank, position, text)
        for rank, chunk in enumerate(chunks):
            for position, sentence in enumerate(sentences(chunk.text)):
                tokens = set(content_tokens(sentence))
                overlap = len(terms & tokens)
                if overlap:
                    # Prefer sentences that cover more of the question, from higher-ranked chunks.
                    score = overlap / len(terms) + 0.01 * (len(chunks) - rank)
                    candidates.append((-score, rank, position, sentence))

        if not candidates:
            return GeneratedAnswer(
                status="insufficient_evidence",
                segments=[AnswerSegment(INSUFFICIENT_EVIDENCE_MESSAGE)],
                citations=[],
                model=self.model_id,
            )

        chosen = sorted(candidates)[: self._max_sentences]
        chosen.sort(key=lambda c: (c[1], c[2]))  # keep document order for readability
        collector = CitationCollector()
        for i, (_, rank, _, sentence) in enumerate(chosen):
            text = sentence if i == len(chosen) - 1 else sentence + " "
            collector.add(text, [Citation(chunk=chunks[rank], cited_text=sentence)])
        return GeneratedAnswer(
            status="answered",
            segments=collector.segments,
            citations=collector.citations,
            model=self.model_id,
            stop_reason="end_turn",
        )
