"""Preview a generation evaluation without generating anything.

``plan_generation`` builds the same fresh index and runs the same retrieval as
``rag eval --generation``, then lists the questions that would reach the generator (the rest
abstain at retrieval and cost nothing), what each question tests, and what the run would
cost. With ``count_tokens=True`` it sends each exact Claude request to the free
token-counting endpoint: that also proves the credentials work and the API accepts the
request, without a billed call. It never calls ``generate``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from production_rag.config import Settings
from production_rag.evaluation.dataset import EvalCase, load_dataset
from production_rag.evaluation.runner import config_snapshot, evaluation_index, select_cases
from production_rag.generation import ExtractiveGenerator
from production_rag.generation.anthropic_generator import AnthropicGenerator

# USD per million tokens (input, output): Anthropic list prices, checked 2026-10.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {"claude-opus-5-5": (4.0, 20.0)}
# Assumed output per answer, including thinking, at effort "medium": short cited answers.
# The worst case assumes every answer runs to RAG_ANTHROPIC_MAX_TOKENS.
TYPICAL_OUTPUT_TOKENS = (300, 1500)
# Fallback when token counting isn't requested or available. Deliberately conservative:
# recent Claude tokenizers produce more tokens per character than older ones, so this
# overestimates input rather than under-budgeting.
_CHARS_PER_TOKEN = 3


def describe_case(case: EvalCase) -> str:
    """What a case tests, in a sentence."""
    parts: list[str] = []
    if not case.answerable:
        if "unanswerable-ontopic" in case.tags:
            parts.append("must decline: the corpus covers the topic but not this fact")
        else:
            parts.append("must decline: off-topic")
    else:
        sources = ", ".join(sorted({r.source for r in case.relevant}))
        parts.append(f"must answer, citing {sources}")
        if case.must_include:
            parts.append("must state " + ", ".join(repr(f) for f in case.must_include))
    if "conflicting" in case.tags:
        parts.append("must follow the official policy, not the unverified forum note")
    if case.must_not_include:
        parts.append("must not output " + ", ".join(repr(f) for f in case.must_not_include))
    return "; ".join(parts)


@dataclass(frozen=True)
class PlannedCase:
    id: str
    split: str | None
    tags: list[str]
    question: str
    measures: str
    calls_generator: bool  # False when retrieval abstains first: no call, no cost
    input_tokens: int | None = None


@dataclass
class GenerationPlan:
    generator: str
    paid: bool
    model: str | None
    effort: str | None
    max_tokens: int | None
    token_source: str  # "count_tokens" (exact, free API) or "estimate" (characters / 3)
    cases: list[PlannedCase] = field(default_factory=list)
    prices_per_mtok: tuple[float, float] | None = None
    retrieval: dict[str, Any] = field(default_factory=dict)  # as the run will configure it

    @property
    def calls(self) -> list[PlannedCase]:
        return [c for c in self.cases if c.calls_generator]

    @property
    def categories(self) -> dict[str, int]:
        """Selected questions per tag, most common first. A question can carry two tags."""
        return dict(Counter(tag for c in self.cases for tag in c.tags).most_common())

    @property
    def in_several_categories(self) -> dict[str, list[str]]:
        return {c.id: c.tags for c in self.cases if len(c.tags) > 1}

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens or 0 for c in self.calls)

    def cost_usd(self) -> dict[str, float] | None:
        """Typical low and high, and the worst case, in USD; None when the price is unknown."""
        if not self.paid or self.prices_per_mtok is None or self.max_tokens is None:
            return None
        price_in, price_out = self.prices_per_mtok
        n = len(self.calls)
        base = self.input_tokens * price_in / 1e6

        def with_output(per_call: int) -> float:
            return round(base + n * per_call * price_out / 1e6, 2)

        low, high = TYPICAL_OUTPUT_TOKENS
        return {
            "input_only": round(base, 2),
            "typical_low": with_output(low),
            "typical_high": with_output(high),
            "worst_case": with_output(self.max_tokens),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "generator": self.generator,
            "paid": self.paid,
            "model": self.model,
            "effort": self.effort,
            "max_tokens": self.max_tokens,
            "token_source": self.token_source,
            "cases": len(self.cases),
            "generator_calls": len(self.calls),
            "abstained_at_retrieval": len(self.cases) - len(self.calls),
            "input_tokens": self.input_tokens,
            "cost_usd": self.cost_usd(),
            "retrieval": self.retrieval,
            "categories": self.categories,
            "in_several_categories": self.in_several_categories,
            "planned": [c.__dict__ for c in self.cases],
        }


def plan_generation(
    settings: Settings,
    *,
    corpus_dir: Path,
    dataset_path: Path,
    split: str | None = None,
    tags: Sequence[str] | None = None,
    count_tokens: bool = False,
    client: anthropic.Anthropic | None = None,
) -> GenerationPlan:
    cases = select_cases(load_dataset(dataset_path), split=split, tags=tags)
    paid = settings.generator == "anthropic"
    claude = AnthropicGenerator(settings, client=client) if paid else None
    plan = GenerationPlan(
        generator=settings.generator,
        paid=paid,
        model=settings.anthropic_model if paid else None,
        effort=settings.anthropic_effort if paid else None,
        max_tokens=settings.anthropic_max_tokens if paid else None,
        token_source="count_tokens" if (paid and count_tokens) else "estimate",
        prices_per_mtok=PRICES_PER_MTOK.get(settings.anthropic_model) if paid else None,
    )
    # The index's own generator is the offline one: nothing in this function can generate.
    with evaluation_index(settings, corpus_dir, generator=ExtractiveGenerator()) as index:
        plan.retrieval = {
            k: v for k, v in config_snapshot(index.container).items() if k != "generator"
        }
        rag = index.container.rag
        for case in cases:
            result = rag.search(case.question)
            calls = not rag.should_abstain(result)
            tokens: int | None = None
            if calls and claude is not None:
                if count_tokens:
                    tokens = claude.count_input_tokens(case.question, result.chunks)
                else:
                    request = claude.build_request(case.question, result.chunks)
                    text = request["system"] + str(request["messages"])
                    tokens = len(text) // _CHARS_PER_TOKEN
            plan.cases.append(
                PlannedCase(
                    id=case.id,
                    split=case.split,
                    tags=case.tags,
                    question=case.question,
                    measures=describe_case(case),
                    calls_generator=calls,
                    input_tokens=tokens,
                )
            )
    return plan
