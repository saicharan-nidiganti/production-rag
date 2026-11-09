from __future__ import annotations

from production_rag.config import Settings
from production_rag.generation.base import (
    INSUFFICIENT_EVIDENCE_MESSAGE,
    AnswerComplete,
    AnswerSegment,
    AnswerStatus,
    Citation,
    CitationAdded,
    GeneratedAnswer,
    GenerationError,
    GenerationEvent,
    Generator,
    TextDelta,
    replay,
)
from production_rag.generation.extractive import ExtractiveGenerator


def build_generator(settings: Settings) -> Generator:
    if settings.generator == "anthropic":
        from production_rag.generation.anthropic_generator import AnthropicGenerator

        return AnthropicGenerator(settings)
    return ExtractiveGenerator()


__all__ = [
    "INSUFFICIENT_EVIDENCE_MESSAGE",
    "AnswerComplete",
    "AnswerSegment",
    "AnswerStatus",
    "Citation",
    "CitationAdded",
    "ExtractiveGenerator",
    "GeneratedAnswer",
    "GenerationError",
    "GenerationEvent",
    "Generator",
    "TextDelta",
    "build_generator",
    "replay",
]
