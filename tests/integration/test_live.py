"""Tests against real external services.

- fastembed (free; `live`, runs with RAG_RUN_LIVE_TESTS=1): downloads the embedding model
  from Hugging Face on first use; set HF_HOME and RAG_FASTEMBED_CACHE_DIR to writable
  directories if the defaults are not writable.
- Claude API (`paid`, runs only with RAG_RUN_PAID_TESTS=1): needs credentials
  (ANTHROPIC_API_KEY or an `ant auth login` profile) and spends a small amount of money.
"""

from __future__ import annotations

import os

import pytest

from production_rag.config import Settings
from production_rag.container import Container, build_container
from production_rag.evaluation import check_gate, load_thresholds, run_evaluation
from tests.conftest import CORPUS_DIR, DATASET, REPO_ROOT

pytestmark = pytest.mark.live


@pytest.mark.paid
def test_claude_answers_with_citations_and_declines_unanswerable(
    corpus_container: Container,
) -> None:
    settings = corpus_container.settings.model_copy(update={"generator": "anthropic"})
    live = build_container(settings)
    try:
        answered = live.rag.answer(
            "How long does the canary run, and how much traffic does it get?"
        )
        assert answered.answer.status == "answered", answered.answer.text
        assert "30 minutes" in answered.answer.text
        assert any(
            c.chunk.source_name == "deployment-runbook.md" for c in answered.answer.citations
        )

        declined = live.rag.answer("How much does the Enterprise plan cost per month?")
        assert declined.answer.status != "answered", declined.answer.text
    finally:
        live.close()


def test_fastembed_hybrid_meets_its_gate() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        embedding_provider="fastembed",
        fastembed_cache_dir=os.environ.get("RAG_FASTEMBED_CACHE_DIR"),
    )
    report = run_evaluation(settings, corpus_dir=CORPUS_DIR, dataset_path=DATASET)
    gate = check_gate(report, load_thresholds(REPO_ROOT / "eval" / "thresholds.fastembed.json"))
    assert gate.passed, gate.failures


def test_fastembed_with_reranker_meets_its_gate() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        embedding_provider="fastembed",
        reranker="fastembed",
        fastembed_cache_dir=os.environ.get("RAG_FASTEMBED_CACHE_DIR"),
    )
    report = run_evaluation(settings, corpus_dir=CORPUS_DIR, dataset_path=DATASET)
    gate = check_gate(report, load_thresholds(REPO_ROOT / "eval" / "thresholds.reranked.json"))
    assert gate.passed, gate.failures
