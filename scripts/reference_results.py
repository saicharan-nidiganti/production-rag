"""Write the reference evaluation reports behind the published retrieval numbers.

Runs golden v2 once per configuration below with the existing harness (`run_evaluation`,
offline: retrieval only, no generator calls, nothing paid) and writes one JSON report per
configuration plus an index (README.md) generated from those reports, into
eval/results/<date>/. Run it from a clean checkout of the commit you want recorded: each
report stores that commit.

    HF_HOME=... RAG_FASTEMBED_CACHE_DIR=... uv run python scripts/reference_results.py 2026-10-29
    uv run python scripts/reference_results.py 2026-10-29 --index-only   # rebuild README.md

The bge-small configurations download two to four models on first use (see
docs/operations.md for the cache variables). Latency depends on the machine and its load:
compare latencies only within one run.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "eval" / "corpus"
DATASET = ROOT / "eval" / "golden_v2.jsonl"
RESULTS = ROOT / "eval" / "results"

_ALTERNATIVE = (
    "An alternative reranker, kept for comparison. ADR-0005 compared it on golden v1; this is "
    "golden v2. It has no calibrated abstention threshold, so it never abstains on its score."
)


@dataclass(frozen=True)
class Configuration:
    name: str
    label: str
    settings: dict[str, Any]
    note: str = ""


def _bge(mode: str = "hybrid", **extra: Any) -> dict[str, Any]:
    return {"embedding_provider": "fastembed", "retrieval_mode": mode, **extra}


CONFIGURATIONS = [
    Configuration("hashing-lexical", "hashing, lexical (BM25 only)", {"retrieval_mode": "lexical"}),
    Configuration("hashing-vector", "hashing, vector", {"retrieval_mode": "vector"}),
    Configuration(
        "hashing-hybrid",
        "hashing, hybrid",
        {"retrieval_mode": "hybrid"},
        "The offline configuration that the CI gate (eval/thresholds.json) checks.",
    ),
    Configuration(
        "bge-small-lexical",
        "bge-small, lexical (BM25 only)",
        _bge("lexical"),
        "Lexical mode computes no vector score, so the embeddings play no part: ranking and "
        "abstention (only when BM25 finds nothing) match hashing, lexical.",
    ),
    Configuration("bge-small-vector", "bge-small, vector", _bge("vector")),
    Configuration("bge-small-hybrid", "bge-small, hybrid", _bge()),
    Configuration(
        "bge-small-hybrid-rerank5",
        "bge-small, hybrid, MiniLM-L-6 reranking 5 candidates",
        _bge(reranker="fastembed", rerank_candidates=5),
        "The recommended configuration and the default candidate count.",
    ),
    Configuration(
        "bge-small-hybrid-rerank10",
        "bge-small, hybrid, MiniLM-L-6 reranking 10 candidates",
        _bge(reranker="fastembed", rerank_candidates=10),
    ),
    Configuration(
        "bge-small-hybrid-rerank20",
        "bge-small, hybrid, MiniLM-L-6 reranking 20 candidates",
        _bge(reranker="fastembed", rerank_candidates=20),
        "The default before 2026-10-29.",
    ),
    Configuration(
        "bge-small-hybrid-rerank20-minilm-l12",
        "bge-small, hybrid, MiniLM-L-12 reranking 20 candidates",
        _bge(
            reranker="fastembed",
            rerank_candidates=20,
            reranker_model="Xenova/ms-marco-MiniLM-L-12-v2",
        ),
        _ALTERNATIVE,
    ),
    Configuration(
        "bge-small-hybrid-rerank20-jina-tiny",
        "bge-small, hybrid, jina-reranker-v1-tiny reranking 20 candidates",
        _bge(
            reranker="fastembed",
            rerank_candidates=20,
            reranker_model="jinaai/jina-reranker-v1-tiny-en",
        ),
        _ALTERNATIVE,
    ),
]


def expected_config(configuration: Configuration) -> dict[str, Any]:
    """What the report's `config` must say for this configuration."""
    s = configuration.settings
    reranked = s.get("reranker") == "fastembed"
    return {
        "embedding_model": (
            "fastembed:BAAI/bge-small-en-v1.5"
            if s.get("embedding_provider") == "fastembed"
            else "hashing-v1-d512"
        ),
        "retrieval_mode": s.get("retrieval_mode", "hybrid"),
        "reranker": s.get("reranker_model", "Xenova/ms-marco-MiniLM-L-6-v2")
        if reranked
        else "none",
        "rerank_candidates": s.get("rerank_candidates") if reranked else None,
    }


def _metric(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def render_index(directory: Path) -> str:
    reports = {
        c.name: json.loads((directory / f"{c.name}.json").read_text(encoding="utf-8"))
        for c in CONFIGURATIONS
        if (directory / f"{c.name}.json").exists()
    }
    commits = sorted({r["code"]["git_commit"] or "unknown" for r in reports.values()})
    first = next(iter(reports.values()))
    lines = [
        f"# Reference evaluation results, {directory.name}",
        "",
        "Raw reports behind the retrieval numbers in the README and docs/evaluation.md, written by",
        f"`uv run python scripts/reference_results.py {directory.name}`. Generated from the",
        "reports in this directory; `tests/unit/test_reference_results.py` checks it stays so.",
        "",
        f"- Dataset: `{first['dataset']['path']}`, {first['dataset']['cases']} questions "
        f"(dev {first['dataset']['splits']['dev']}, test {first['dataset']['splits']['test']}), "
        f"SHA-256 `{first['dataset']['sha256'][:12]}…`",
        f"- Corpus: {first['corpus']['documents']} documents, fingerprint "
        f"`{first['corpus']['fingerprint'][:12]}…`",
        f"- Code: {', '.join(f'`{c[:7]}`' for c in commits)}",
        "- Retrieval only, top 5, offline: no generator was called and nothing was paid for.",
        "",
        "| Configuration | Report | Test hit rate | Test MRR | Dev hit rate | Dev MRR "
        "| Unanswerable abstained (test) | False abstention | Retrieval p50 / p95 ms "
        "| Rerank p50 ms |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in CONFIGURATIONS:
        report = reports.get(c.name)
        if report is None:
            continue
        summary = report["summary"]
        test, dev = summary["splits"]["test"], summary["splits"]["dev"]
        latency = summary["latency_ms"]
        lines.append(
            f"| {c.label} | [{c.name}.json]({c.name}.json) "
            f"| {_metric(test['retrieval']['hit_rate'])} | {_metric(test['retrieval']['mrr'])} "
            f"| {_metric(dev['retrieval']['hit_rate'])} | {_metric(dev['retrieval']['mrr'])} "
            f"| {_metric(test['abstention']['unanswerable_abstention_rate'])} "
            f"| {_metric(summary['abstention']['false_abstention_rate'])} "
            f"| {_ms(latency['retrieval_p50'])} / {_ms(latency['retrieval_p95'])} "
            f"| {_ms(latency.get('rerank_p50'))} |"
        )
    lines += ["", "## Notes and limitations", ""]
    lines += [f"- **{c.label}**: {c.note}" for c in CONFIGURATIONS if c.note and c.name in reports]
    lines += [
        "- The 94 questions were written alongside a 9-document synthetic corpus: these are a",
        "  regression baseline for this project, not a benchmark.",
        "- Latency is from one laptop CPU run, with other work possible on the machine; compare",
        "  latencies only within this table, and expect them to vary between runs.",
        "- Answer quality isn't measured here: the Claude evaluation hasn't run.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("date", help="directory name under eval/results/, e.g. 2026-10-29")
    parser.add_argument("--index-only", action="store_true", help="only rebuild README.md")
    args = parser.parse_args()
    directory = RESULTS / args.date
    directory.mkdir(parents=True, exist_ok=True)
    if not args.index_only:
        from production_rag.config import Settings
        from production_rag.evaluation import run_evaluation

        cache = os.environ.get("RAG_FASTEMBED_CACHE_DIR")
        for c in CONFIGURATIONS:
            settings = Settings(_env_file=None, fastembed_cache_dir=cache, **c.settings)
            report = run_evaluation(settings, corpus_dir=CORPUS, dataset_path=DATASET)
            report["dataset"]["path"] = DATASET.relative_to(ROOT).as_posix()
            report["corpus"]["path"] = CORPUS.relative_to(ROOT).as_posix()
            (directory / f"{c.name}.json").write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            print(f"wrote {c.name}.json")
    (directory / "README.md").write_text(render_index(directory), encoding="utf-8", newline="\n")
    print(f"wrote {directory / 'README.md'}")


if __name__ == "__main__":
    main()
