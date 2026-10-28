"""Reproduce the comparison tables in docs/evaluation.md and ADR-0004/0005.

Runs golden v2 for each retrieval configuration (embedding provider x mode, with and without
the cross-encoder reranker), the abstention calibration, and a sweep of the extractive
generator's minimum-coverage setting, then prints Markdown tables. Thresholds are
calibrated on the dev split; the test split is held out, so its numbers show how they
generalize. Everything here is free: the generator is the offline extractive baseline.

    uv run python scripts/compare_configs.py
    uv run python scripts/compare_configs.py --providers hashing   # skip the model downloads

The fastembed rows download the embedding and reranker models on first use (see
docs/operations.md for HF_HOME and RAG_FASTEMBED_CACHE_DIR).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from production_rag.config import Settings
from production_rag.evaluation import run_evaluation
from production_rag.generation import ExtractiveGenerator

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "eval" / "corpus"
DATASET = ROOT / "eval" / "golden_v2.jsonl"


def _settings(**overrides: Any) -> Settings:
    cache = os.environ.get("RAG_FASTEMBED_CACHE_DIR")
    return Settings(_env_file=None, fastembed_cache_dir=cache, **overrides)


def _run(settings: Settings, **kwargs: Any) -> dict[str, Any]:
    return run_evaluation(settings, corpus_dir=CORPUS, dataset_path=DATASET, **kwargs)


def _f(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _configs(providers: list[str]) -> list[tuple[str, str, dict[str, Any]]]:
    rows = []
    for provider in providers:
        for mode in ("lexical", "vector", "hybrid"):
            rows.append((provider, mode, {"embedding_provider": provider, "retrieval_mode": mode}))
        if provider == "fastembed":
            rows.append(
                (
                    provider,
                    "hybrid + rerank",
                    {"embedding_provider": provider, "reranker": provider},
                )
            )
    return rows


def retrieval_table(providers: list[str]) -> None:
    print(
        "| Embeddings | Mode | Test hit rate | Test MRR | Dev hit rate | Dev MRR "
        "| Unanswerable abstained (test) | False abstention | Retrieval p95 |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for provider, mode, overrides in _configs(providers):
        summary = _run(_settings(**overrides))["summary"]
        test, dev = summary["splits"]["test"], summary["splits"]["dev"]
        print(
            f"| {provider} | {mode} | {_f(test['retrieval']['hit_rate'])} "
            f"| {_f(test['retrieval']['mrr'])} | {_f(dev['retrieval']['hit_rate'])} "
            f"| {_f(dev['retrieval']['mrr'])} "
            f"| {_f(test['abstention']['unanswerable_abstention_rate'])} "
            f"| {_f(summary['abstention']['false_abstention_rate'])} "
            f"| {summary['latency_ms']['retrieval_p95']:.0f} ms |"
        )


def abstention_table(providers: list[str]) -> None:
    print(
        "| Signal | Split | Lowest answerable | Off-topic unanswerable "
        "| On-topic unanswerable | Threshold |"
    )
    print("| --- | --- | --- | --- | --- | --- |")
    signals: list[tuple[str, str, dict[str, Any]]] = [
        (f"{p} cosine", "best_vector_score", {"embedding_provider": p}) for p in providers
    ]
    if "fastembed" in providers:
        reranked = {"embedding_provider": "fastembed", "reranker": "fastembed"}
        signals.append(("rerank score", "best_rerank_score", reranked))
    for name, field, overrides in signals:
        settings = _settings(**overrides)
        rerank_threshold = settings.rerank_abstain_threshold
        threshold = rerank_threshold if field == "best_rerank_score" else settings.abstain_threshold
        per_query = _run(settings)["per_query"]
        for split in ("dev", "test"):
            rows = [q for q in per_query if q["split"] == split]
            answerable = min(q["retrieval"][field] for q in rows if q["answerable"])
            off = [q["retrieval"][field] for q in rows if "unanswerable-offtopic" in q["tags"]]
            on = [q["retrieval"][field] for q in rows if "unanswerable-ontopic" in q["tags"]]
            print(
                f"| {name} | {split} | {answerable:.2f} | {min(off):.2f} to {max(off):.2f} "
                f"| {min(on):.2f} to {max(on):.2f} | {threshold} |"
            )


def extractive_sweep(providers: list[str]) -> None:
    print(
        "| Embeddings | Min coverage | Answered | Unanswerable declined "
        "| Citation precision | Fact recall |"
    )
    print("| --- | --- | --- | --- | --- | --- |")
    for provider in providers:
        settings = _settings(embedding_provider=provider)
        for coverage in (0.0, 0.25, 0.34, 0.5):
            report = _run(
                settings, with_generation=True, generator=ExtractiveGenerator(min_coverage=coverage)
            )
            g = report["summary"]["splits"]["test"]["generation"]
            print(
                f"| {provider} | {coverage:.2f} | {_f(g['answered_rate'])} "
                f"| {_f(g['unanswerable_declined_rate'])} | {_f(g['citation_precision'])} "
                f"| {_f(g['fact_recall'])} |"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--providers", nargs="+", default=["hashing", "fastembed"], choices=["hashing", "fastembed"]
    )
    args = parser.parse_args()
    for title, table in (
        ("Retrieval by configuration (golden v2)", retrieval_table),
        ("Retrieval-level abstention signals", abstention_table),
        ("Extractive generator: minimum-coverage sweep (test split)", extractive_sweep),
    ):
        print(f"\n### {title}\n")
        table(args.providers)


if __name__ == "__main__":
    main()
