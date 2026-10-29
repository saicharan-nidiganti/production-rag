"""Evaluation runner.

Every run builds a fresh, temporary index from the corpus with the current settings, so a
result depends only on (corpus, dataset, configuration, code) and is reproducible.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from production_rag import __version__
from production_rag.config import Settings
from production_rag.container import Container, build_container
from production_rag.db.migrate import upgrade_to_head
from production_rag.evaluation.dataset import EvalCase, load_dataset
from production_rag.evaluation.metrics import (
    citation_precision,
    fact_recall,
    forbidden_absent,
    mean,
    percentile,
    score_retrieval,
)
from production_rag.generation import GenerationError, Generator
from production_rag.ingestion import discover_documents
from production_rag.observability import Timer
from production_rag.rag import RagService
from production_rag.retrieval import SearchResult

REPORT_SCHEMA_VERSION = 1


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def corpus_fingerprint(files: list[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in files:
        digest.update(f"{path.relative_to(root).as_posix()}\0{_sha256_file(path)}\n".encode())
    return digest.hexdigest()


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - resolved from PATH on purpose
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def config_snapshot(container: Container) -> dict[str, Any]:
    s = container.settings
    return {
        "embedding_model": container.embedder.model_id,
        "retrieval_mode": s.retrieval_mode,
        "top_k": s.top_k,
        "candidate_pool": s.candidate_pool,
        "rrf_k": s.rrf_k,
        "chunk_size": s.chunk_size,
        "chunk_overlap": s.chunk_overlap,
        "abstain_threshold": s.abstain_threshold,
        "reranker": s.reranker_model if s.reranker != "none" else "none",
        "rerank_candidates": s.rerank_candidates if s.reranker != "none" else None,
        "rerank_abstain_threshold": s.rerank_abstain_threshold,
        "generator": container.rag.generator.model_id,
    }


@dataclass
class IndexedCorpus:
    container: Container
    files: list[Path]
    stats: dict[str, int]


@contextmanager
def evaluation_index(
    settings: Settings, corpus_dir: Path, *, generator: Generator | None = None
) -> Iterator[IndexedCorpus]:
    """A fresh temporary index of the corpus, built with ``settings``."""
    if not corpus_dir.is_dir():
        raise ValueError(f"corpus directory not found: {corpus_dir}")
    files = discover_documents(corpus_dir)
    if not files:
        raise ValueError(f"no supported documents found in {corpus_dir}")
    # ignore_cleanup_errors: on Windows a lingering SQLite handle would otherwise fail the run.
    with tempfile.TemporaryDirectory(prefix="rag-eval-", ignore_cleanup_errors=True) as tmp:
        db_url = f"sqlite:///{Path(tmp, 'eval.db').as_posix()}"
        upgrade_to_head(db_url)
        container = build_container(
            settings.model_copy(update={"database_url": db_url}), generator=generator
        )
        try:
            for path in files:
                container.ingestion.ingest(path.name, path.read_bytes())
            yield IndexedCorpus(container, files, container.index_stats())
        finally:
            container.close()


def select_cases(
    cases: list[EvalCase], *, split: str | None = None, tags: Sequence[str] | None = None
) -> list[EvalCase]:
    """Cases in ``split`` (all when None) that carry any of ``tags`` (all when empty)."""
    wanted = set(tags or ())
    selected = [
        c
        for c in cases
        if (split is None or c.split == split) and (not wanted or wanted & set(c.tags))
    ]
    if not selected:
        raise ValueError(f"no cases match split={split!r} tags={sorted(wanted)}")
    return selected


def run_evaluation(
    settings: Settings,
    *,
    corpus_dir: Path,
    dataset_path: Path,
    with_generation: bool = False,
    generator: Generator | None = None,
    split: str | None = None,
    tags: Sequence[str] | None = None,
) -> dict[str, Any]:
    cases = select_cases(load_dataset(dataset_path), split=split, tags=tags)
    with evaluation_index(settings, corpus_dir, generator=generator) as index:
        per_query = [_evaluate_case(index.container, case, with_generation) for case in cases]
        config = config_snapshot(index.container)
        files, stats = index.files, index.stats

    summary = summarize(per_query, with_generation)
    splits = sorted({c.split for c in cases if c.split is not None})
    if splits:
        summary["splits"] = {
            split: summarize([q for q in per_query if q["split"] == split], with_generation)
            for split in splits
        }
    present_tags = sorted({t for c in cases for t in c.tags})

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "code": {"version": __version__, "git_commit": _git_commit()},
        "dataset": {
            "path": dataset_path.as_posix(),
            "sha256": _sha256_file(dataset_path),
            "cases": len(cases),
            "answerable": sum(1 for c in cases if c.answerable),
            "splits": {s: sum(1 for c in cases if c.split == s) for s in splits},
            "selection": {"split": split, "tags": sorted(tags or [])},
        },
        "corpus": {
            "path": corpus_dir.as_posix(),
            "fingerprint": corpus_fingerprint(files, corpus_dir),
            "documents": stats["documents"],
            "chunks": stats["chunks"],
        },
        "config": config,
        "with_generation": with_generation,
        "summary": summary,
        "by_tag": {
            tag: summarize([q for q in per_query if tag in q["tags"]], with_generation)
            for tag in present_tags
        },
        "per_query": per_query,
    }


def _evaluate_case(container: Container, case: EvalCase, with_generation: bool) -> dict[str, Any]:
    rag = container.rag
    record: dict[str, Any] = {
        "id": case.id,
        "question": case.question,
        "answerable": case.answerable,
        "split": case.split,
        "tags": case.tags,
    }
    if with_generation:
        try:
            result = rag.answer(case.question)
        except GenerationError as exc:
            # One failed case (an outage, a rejected request) is recorded, not fatal: the
            # rest of a paid run still counts. Retrieval is scored on its own.
            record["generation"] = _failed_generation(exc)
            timer = Timer()
            with timer.stage("retrieval"):
                retrieval = rag.search(case.question)
            record["retrieval"] = _retrieval_record(rag, case, retrieval, timer.timings_ms)
            return record
        retrieval, timings = result.retrieval, result.timings_ms
        answer = result.answer
        record["generation"] = {
            "status": answer.status,
            "answer": answer.text,
            "citations": [
                {"source": c.chunk.source_name, "heading": c.chunk.heading}
                for c in answer.citations
            ],
            "citation_precision": citation_precision(answer, case.relevant)
            if case.answerable
            else None,
            "fact_recall": fact_recall(answer.text, case.must_include),
            "forbidden_absent": forbidden_absent(answer.text, case.must_not_include),
            "stop_reason": answer.stop_reason,  # "max_tokens" means the answer was cut off
            "usage": answer.usage,  # billed tokens, when a hosted model answered
            "ms": timings.get("generation"),
        }
    else:
        timer = Timer()
        with timer.stage("retrieval"):
            retrieval = rag.search(case.question)
        timings = timer.timings_ms

    record["retrieval"] = _retrieval_record(rag, case, retrieval, timings)
    return record


def _failed_generation(exc: GenerationError) -> dict[str, Any]:
    return {
        "status": "error",
        "error": str(exc),
        "retryable": exc.retryable,
        "answer": None,
        "citations": [],
        "citation_precision": None,
        "fact_recall": None,
        "forbidden_absent": None,
        "stop_reason": None,
        "usage": None,
        "ms": None,
    }


def _retrieval_record(
    rag: RagService, case: EvalCase, retrieval: SearchResult, timings: dict[str, float]
) -> dict[str, Any]:
    scores = score_retrieval(retrieval.chunks, case.relevant)
    return {
        "hit": scores.hit,
        "recall": scores.recall,
        "reciprocal_rank": scores.reciprocal_rank,
        "precision": scores.precision,
        "first_relevant_rank": scores.first_relevant_rank,
        "best_vector_score": retrieval.best_vector_score,
        "best_rerank_score": retrieval.best_rerank_score,
        "abstained": rag.should_abstain(retrieval),
        "retrieved": [{"source": c.source_name, "heading": c.heading} for c in retrieval.chunks],
        "ms": timings["retrieval"],
        "rerank_ms": retrieval.rerank_ms,  # included in "ms"
    }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def summarize(per_query: list[dict[str, Any]], with_generation: bool) -> dict[str, Any]:
    answerable = [q for q in per_query if q["answerable"]]
    unanswerable = [q for q in per_query if not q["answerable"]]
    retrieval_ms = [q["retrieval"]["ms"] for q in per_query]
    summary: dict[str, Any] = {
        "retrieval": {
            "hit_rate": _round(mean([q["retrieval"]["hit"] for q in answerable])),
            "recall": _round(mean([q["retrieval"]["recall"] for q in answerable])),
            "mrr": _round(mean([q["retrieval"]["reciprocal_rank"] for q in answerable])),
            "precision": _round(mean([q["retrieval"]["precision"] for q in answerable])),
        },
        "abstention": {
            "false_abstention_rate": _round(
                mean([1.0 if q["retrieval"]["abstained"] else 0.0 for q in answerable])
            ),
            "unanswerable_abstention_rate": _round(
                mean([1.0 if q["retrieval"]["abstained"] else 0.0 for q in unanswerable])
            ),
        },
        "latency_ms": {
            "retrieval_p50": percentile(retrieval_ms, 50),
            "retrieval_p95": percentile(retrieval_ms, 95),
        },
    }
    rerank_ms = [q["retrieval"]["rerank_ms"] for q in per_query if q["retrieval"]["rerank_ms"]]
    if rerank_ms:
        summary["latency_ms"]["rerank_p50"] = percentile(rerank_ms, 50)
        summary["latency_ms"]["rerank_p95"] = percentile(rerank_ms, 95)
    if with_generation:
        failed = [q for q in per_query if q["generation"]["status"] == "error"]
        # Rates are over the cases that completed; `failed` says how many didn't, and an
        # acceptance gate should require it to be 0.
        completed = [q for q in per_query if q["generation"]["status"] != "error"]
        answerable = [q for q in completed if q["answerable"]]
        unanswerable = [q for q in completed if not q["answerable"]]
        gen_ms = [q["generation"]["ms"] for q in per_query if q["generation"]["ms"] is not None]
        summary["generation"] = {
            "answered_rate": _round(
                mean([1.0 if q["generation"]["status"] == "answered" else 0.0 for q in answerable])
            ),
            "unanswerable_declined_rate": _round(
                mean(
                    [0.0 if q["generation"]["status"] == "answered" else 1.0 for q in unanswerable]
                )
            ),
            "citation_precision": _round(
                mean([q["generation"]["citation_precision"] for q in answerable])
            ),
            "fact_recall": _round(mean([q["generation"]["fact_recall"] for q in answerable])),
            # Share of cases with forbidden strings (planted injections) whose answer has none.
            "injection_resisted_rate": _round(
                mean([q["generation"]["forbidden_absent"] for q in completed])
            ),
            "failed": len(failed),
            "failed_ids": [q["id"] for q in failed],
            "truncated": sum(
                1 for q in per_query if q["generation"]["stop_reason"] == "max_tokens"
            ),
            "usage": {
                kind: sum((q["generation"]["usage"] or {}).get(kind, 0) for q in per_query)
                for kind in ("input_tokens", "output_tokens")
            },
        }
        summary["latency_ms"]["generation_p50"] = percentile(gen_ms, 50)
        summary["latency_ms"]["generation_p95"] = percentile(gen_ms, 95)
    return summary
