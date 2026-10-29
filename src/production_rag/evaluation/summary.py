"""A human-readable summary of an evaluation report (what `rag eval` prints by default)."""

from __future__ import annotations

from typing import Any

_RETRIEVAL_ROWS = [
    ("Hit rate", "retrieval", "hit_rate"),
    ("Recall", "retrieval", "recall"),
    ("MRR", "retrieval", "mrr"),
    ("Precision", "retrieval", "precision"),
    ("False abstention", "abstention", "false_abstention_rate"),
    ("Unanswerable abstained", "abstention", "unanswerable_abstention_rate"),
]
_GENERATION_ROWS = [
    ("Answered", "answered_rate"),
    ("Unanswerable declined", "unanswerable_declined_rate"),
    ("Citation precision", "citation_precision"),
    ("Fact recall", "fact_recall"),
    ("Injection resisted", "injection_resisted_rate"),
    ("Failed cases", "failed"),
    ("Truncated answers", "truncated"),
]


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def format_summary(report: dict[str, Any]) -> str:
    dataset, corpus, config = report["dataset"], report["corpus"], report["config"]
    summary = report["summary"]
    splits = summary.get("splits", {})
    columns = [("all", summary)] + [(name, splits[name]) for name in sorted(splits)]
    split_counts = ", ".join(f"{name} {count}" for name, count in dataset["splits"].items())
    reranker = config["reranker"]
    if reranker != "none":
        reranker += f" ({config['rerank_candidates']} candidates)"
    lines = [
        f"Dataset   {dataset['path']}: {dataset['cases']} questions"
        + (f" ({split_counts})" if split_counts else ""),
        f"Corpus    {corpus['documents']} documents, {corpus['chunks']} chunks",
        f"Retrieval {config['embedding_model']}, {config['retrieval_mode']}, "
        f"top_k {config['top_k']}, reranker {reranker}",
        f"Generator {config['generator']}"
        + ("" if report["with_generation"] else " (not run: retrieval only)"),
        f"Code      {report['code']['version']} at {report['code']['git_commit'] or 'unknown'}",
        "",
    ]
    header = f"{'':<24}" + "".join(f"{name:>8}" for name, _ in columns)
    lines.append(header)
    for label, group, key in _RETRIEVAL_ROWS:
        lines.append(f"{label:<24}" + "".join(f"{_cell(s[group][key]):>8}" for _, s in columns))
    if report["with_generation"]:
        for label, key in _GENERATION_ROWS:
            lines.append(
                f"{label:<24}" + "".join(f"{_cell(s['generation'][key]):>8}" for _, s in columns)
            )
    latency = summary["latency_ms"]
    lines.append("")
    lines.append(
        "Latency   "
        + ", ".join(
            f"{name.replace('_', ' ')} {value:.0f} ms"
            for name, value in latency.items()
            if value is not None
        )
    )
    if report["with_generation"]:
        generation = summary["generation"]
        usage = generation["usage"]
        lines.append(
            f"Tokens    {usage['input_tokens']} input, {usage['output_tokens']} output (billed)"
        )
        if generation["failed_ids"]:
            lines.append("Failed    " + ", ".join(generation["failed_ids"]))
    return "\n".join(lines)
