"""Reproduce the comparison tables in docs/evaluation.md and ADR-0004.

Runs the golden dataset for every embedding provider x retrieval mode, plus a sweep of the
extractive generator's minimum-coverage setting, and prints Markdown tables.

    uv run python scripts/compare_configs.py
    uv run python scripts/compare_configs.py --providers hashing   # skip the model download

The fastembed rows download the embedding model on first use (see docs/operations.md for
HF_HOME and RAG_FASTEMBED_CACHE_DIR).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from production_rag.config import Settings
from production_rag.evaluation import run_evaluation
from production_rag.generation import ExtractiveGenerator

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "eval" / "corpus"
DATASET = ROOT / "eval" / "golden_v1.jsonl"


def _run(settings: Settings, **kwargs: Any) -> dict[str, Any]:
    return run_evaluation(settings, corpus_dir=CORPUS, dataset_path=DATASET, **kwargs)


def retrieval_table(providers: list[str]) -> None:
    print("| Embeddings | Mode | Hit rate | Recall | MRR | Retrieval p95 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for provider in providers:
        for mode in ("lexical", "vector", "hybrid"):
            settings = Settings(_env_file=None, embedding_provider=provider, retrieval_mode=mode)
            summary = _run(settings)["summary"]
            r = summary["retrieval"]
            print(
                f"| {provider} | {mode} | {r['hit_rate']:.3f} | {r['recall']:.3f} | "
                f"{r['mrr']:.3f} | {summary['latency_ms']['retrieval_p95']:.1f} ms |"
            )


def abstention_table(providers: list[str]) -> None:
    print("| Provider | Lowest answerable best-cosine | Unanswerable best-cosines | Threshold |")
    print("| --- | --- | --- | --- |")
    for provider in providers:
        settings = Settings(_env_file=None, embedding_provider=provider, retrieval_mode="vector")
        per_query = _run(settings)["per_query"]
        answerable = min(q["retrieval"]["best_vector_score"] for q in per_query if q["answerable"])
        unanswerable = sorted(
            q["retrieval"]["best_vector_score"] for q in per_query if not q["answerable"]
        )
        print(
            f"| {provider} | {answerable:.3f} | {unanswerable[0]:.3f} to {unanswerable[-1]:.3f} | "
            f"{settings.abstain_threshold} |"
        )


def extractive_sweep(providers: list[str]) -> None:
    print(
        "| Embeddings | Min coverage | Answered | Unanswerable declined "
        "| Citation precision | Fact recall |"
    )
    print("| --- | --- | --- | --- | --- | --- |")
    for provider in providers:
        settings = Settings(_env_file=None, embedding_provider=provider)
        for coverage in (0.0, 0.25, 0.34, 0.5):
            report = _run(
                settings,
                with_generation=True,
                generator=ExtractiveGenerator(min_coverage=coverage),
            )
            g = report["summary"]["generation"]
            print(
                f"| {provider} | {coverage:.2f} | {g['answered_rate']:.3f} | "
                f"{g['unanswerable_declined_rate']:.3f} | {g['citation_precision']:.3f} | "
                f"{g['fact_recall']:.3f} |"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--providers", nargs="+", default=["hashing", "fastembed"], choices=["hashing", "fastembed"]
    )
    args = parser.parse_args()
    for title, table in (
        ("Retrieval by embedder and mode", retrieval_table),
        ("Retrieval-level abstention calibration", abstention_table),
        ("Extractive generator: minimum-coverage sweep (hybrid retrieval)", extractive_sweep),
    ):
        print(f"\n### {title}\n")
        table(args.providers)


if __name__ == "__main__":
    main()
