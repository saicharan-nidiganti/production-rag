"""Command-line interface: ``rag <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from production_rag.config import Settings, get_settings
from production_rag.observability import configure_logging

if TYPE_CHECKING:
    from production_rag.container import Container


def _container(settings: Settings) -> Container:
    # Imported lazily so `rag --help` and `rag db` don't load numpy, models or the SDK.
    from production_rag.container import build_container

    return build_container(settings)


def cmd_db(args: argparse.Namespace, settings: Settings) -> int:
    from production_rag.db import create_db_engine
    from production_rag.db.migrate import current_revision, head_revision, upgrade_to_head

    if args.action == "upgrade":
        upgrade_to_head(settings.database_url)
    engine = create_db_engine(settings.database_url)
    try:
        print(json.dumps({"current": current_revision(engine), "head": head_revision()}))
    finally:
        engine.dispose()
    return 0


def cmd_ingest(args: argparse.Namespace, settings: Settings) -> int:
    from production_rag.ingestion import (
        ExtractionError,
        UnsupportedDocumentError,
        discover_documents,
    )

    paths = [found for raw in args.paths for found in discover_documents(Path(raw))]
    container = _container(settings)
    failures = 0
    try:
        for path in paths:
            try:
                result = container.ingestion.ingest(path.name, path.read_bytes())
            except (OSError, ExtractionError, UnsupportedDocumentError, ValueError) as exc:
                failures += 1
                print(json.dumps({"path": str(path), "error": str(exc)}))
                continue
            doc = result.document
            print(
                json.dumps(
                    {
                        "path": str(path),
                        "document_id": doc.id,
                        "created": result.created,
                        "status": doc.status,
                        "chunks": doc.num_chunks,
                    }
                )
            )
    finally:
        container.close()
    return 1 if failures else 0


def cmd_search(args: argparse.Namespace, settings: Settings) -> int:
    container = _container(settings)
    try:
        result = container.rag.search(args.query, top_k=args.top_k, mode=args.mode)
    finally:
        container.close()
    for rank, chunk in enumerate(result.chunks, start=1):
        print(
            f"{rank}. [{chunk.score:.4f}] {chunk.source_name} - {chunk.heading or '(no heading)'}"
        )
        print(f"   {' '.join(chunk.text.split())[:200]}")
    if not result.chunks:
        print("no results")
    return 0


def _upstream_detail(exc: BaseException) -> str:
    """The API's own error message, for operators at the CLI. The HTTP API never returns it,
    because it can echo request content."""
    import anthropic

    cause = exc.__cause__
    if isinstance(cause, anthropic.APIStatusError) and isinstance(cause.body, dict):
        message = (cause.body.get("error") or {}).get("message")
        if message:
            return f" (API said: {message})"
    return ""


def cmd_ask(args: argparse.Namespace, settings: Settings) -> int:
    from production_rag.generation import GenerationError

    container = _container(settings)
    try:
        result = container.rag.answer(args.question, top_k=args.top_k, mode=args.mode)
    except GenerationError as exc:
        print(f"generation failed: {exc}{_upstream_detail(exc)}", file=sys.stderr)
        return 1
    finally:
        container.close()
    answer = result.answer
    print(f"[{answer.status} | {answer.model}]\n")
    for segment in answer.segments:
        marks = "".join(f"[{i + 1}]" for i in segment.citations)
        text = segment.text.rstrip()  # markers go after the sentence, before its spacing
        print(f"{text}{marks}{segment.text[len(text) :]}", end="")
    print("\n")
    for i, citation in enumerate(answer.citations, start=1):
        chunk = citation.chunk
        print(f"[{i}] {chunk.source_name} - {chunk.heading or '(no heading)'}")
    return 0


def cmd_reindex(_: argparse.Namespace, settings: Settings) -> int:
    container = _container(settings)
    try:
        updated = container.ingestion.reindex()
        print(json.dumps({"reembedded_chunks": updated, **container.index_stats()}))
    finally:
        container.close()
    return 0


def cmd_eval(args: argparse.Namespace, settings: Settings) -> int:
    from production_rag.evaluation import check_gate, load_thresholds, run_evaluation

    overrides: dict[str, object] = {}
    if args.mode:
        overrides["retrieval_mode"] = args.mode
    if args.top_k:
        overrides["top_k"] = args.top_k
    settings = settings.model_copy(update=overrides)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
    if args.plan:
        return _print_plan(args, settings, tags)
    if args.generation and settings.generator == "anthropic" and not args.allow_paid:
        # .env.example selects the Claude generator, so a plain `rag eval --generation`
        # would otherwise spend money on every question without anyone deciding to.
        print(
            "refusing to run: --generation with RAG_GENERATOR=anthropic calls the paid Claude "
            "API once per question. Pass --allow-paid to proceed, or set "
            "RAG_GENERATOR=extractive for a free run.",
            file=sys.stderr,
        )
        return 2
    report = run_evaluation(
        settings,
        corpus_dir=Path(args.corpus),
        dataset_path=Path(args.dataset),
        with_generation=args.generation,
        split=args.split,
        tags=tags,
    )

    if args.output:
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"{stamp}-{report['config']['retrieval_mode']}.json"
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"report: {out_path}")

    if args.json:
        print(json.dumps({"config": report["config"], "summary": report["summary"]}, indent=2))
    else:
        from production_rag.evaluation.summary import format_summary

        print(format_summary(report))

    if args.thresholds:
        gate = check_gate(report, load_thresholds(Path(args.thresholds)))
        for line in gate.checks:
            print(f"PASS {line}")
        for line in gate.failures:
            print(f"FAIL {line}")
        print("gate: passed" if gate.passed else "gate: FAILED")
        return 0 if gate.passed else 1
    return 0


def _run_command(args: argparse.Namespace, settings: Settings) -> str:
    """The command that runs exactly the planned evaluation, independent of .env."""
    env = [
        f"RAG_EMBEDDING_PROVIDER={settings.embedding_provider}",
        f"RAG_RERANKER={settings.reranker}",
        f"RAG_GENERATOR={settings.generator}",
        f"RAG_ANTHROPIC_MODEL={settings.anthropic_model}",
        f"RAG_ANTHROPIC_EFFORT={settings.anthropic_effort}",
        f"RAG_ANTHROPIC_MAX_TOKENS={settings.anthropic_max_tokens}",
    ]
    cmd = ["uv run rag eval --generation"]
    if settings.generator == "anthropic":
        cmd.append("--allow-paid")
    cmd.append(f"--dataset {args.dataset}")
    if args.split:
        cmd.append(f"--split {args.split}")
    if args.tags:
        cmd.append(f"--tags {args.tags}")
    if args.mode:
        cmd.append(f"--mode {args.mode}")
    if args.top_k:
        cmd.append(f"--top-k {args.top_k}")
    cmd.append("--thresholds eval/thresholds.claude.json --output eval/reports")
    return " ".join(env + cmd)


def _print_plan(args: argparse.Namespace, settings: Settings, tags: list[str] | None) -> int:
    from production_rag.evaluation.plan import TYPICAL_OUTPUT_TOKENS, plan_generation
    from production_rag.generation import GenerationError

    try:
        plan = plan_generation(
            settings,
            corpus_dir=Path(args.corpus),
            dataset_path=Path(args.dataset),
            split=args.split,
            tags=tags,
            count_tokens=args.count_tokens,
        )
    except GenerationError as exc:  # only token counting talks to the API, and it's free
        print(f"token counting failed: {exc}{_upstream_detail(exc)}", file=sys.stderr)
        return 1
    summary = plan.to_dict()
    print("Generation evaluation plan (nothing was generated)")
    print(
        f"  generator:        {plan.generator}"
        + (
            f" ({plan.model}, effort {plan.effort}, max_tokens {plan.max_tokens})"
            if plan.paid
            else ""
        )
    )
    r = plan.retrieval
    reranker = (
        f"reranker {r['reranker']} on {r['rerank_candidates']} candidates"
        if r["reranker"] != "none"
        else "no reranker"
    )
    print(
        f"  retrieval:        {r['embedding_model']}, {r['retrieval_mode']}, top_k {r['top_k']}; "
        f"{reranker}; abstains below cosine {r['abstain_threshold']}"
        + (
            f" or rerank score {r['rerank_abstain_threshold']}"
            if r["rerank_abstain_threshold"] is not None
            else ""
        )
    )
    print(f"  questions:        {summary['cases']}")
    categories = ", ".join(f"{tag} {n}" for tag, n in plan.categories.items())
    several = "; ".join(f"{i} is {' + '.join(t)}" for i, t in plan.in_several_categories.items())
    print(f"  categories:       {categories}" + (f" ({several})" if several else ""))
    abstained = summary["abstained_at_retrieval"]
    print(
        f"  generator calls:  {summary['generator_calls']} "
        f"({abstained} abstain at retrieval and cost nothing)"
    )
    if plan.paid:
        exact = plan.token_source == "count_tokens"  # noqa: S105 - a mode name, not a secret
        how = "exact, from the free token-counting API" if exact else "conservative estimate"
        print(f"  input tokens:     {plan.input_tokens:,} ({how})")
        cost = plan.cost_usd()
        if cost is None:
            print(f"  cost:             unknown (no price on file for {plan.model})")
        else:
            low, high = TYPICAL_OUTPUT_TOKENS
            print(
                f"  estimated cost:   ${cost['typical_low']:.2f} to ${cost['typical_high']:.2f} "
                f"(input ${cost['input_only']:.2f} + {low}-{high} output tokens per answer)"
            )
            print(
                f"  worst case:       ${cost['worst_case']:.2f} "
                f"(every answer runs to max_tokens={plan.max_tokens})"
            )
    else:
        print("  cost:             $0 (offline generator)")
    print(f"  command:          {_run_command(args, settings)}")
    print()
    print("| id | split | calls | input tokens | tests |")
    print("| --- | --- | --- | --- | --- |")
    for case in plan.cases:
        tokens = "-" if case.input_tokens is None else str(case.input_tokens)
        calls = "yes" if case.calls_generator else "no (abstains)"
        print(f"| {case.id} | {case.split or '-'} | {calls} | {tokens} | {case.measures} |")
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    from production_rag.api import create_app

    if args.migrate:
        from production_rag.db.migrate import upgrade_to_head

        upgrade_to_head(settings.database_url)
    # access_log=False: the request middleware already logs each request with its request id.
    uvicorn.run(
        create_app(settings), host=args.host, port=args.port, log_config=None, access_log=False
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rag", description="Production RAG command-line tools")
    sub = parser.add_subparsers(dest="command", required=True)

    db = sub.add_parser("db", help="database migrations")
    db.add_argument("action", choices=["upgrade", "current"])
    db.set_defaults(func=cmd_db)

    ingest = sub.add_parser("ingest", help="ingest files or directories")
    ingest.add_argument("paths", nargs="+")
    ingest.set_defaults(func=cmd_ingest)

    modes = ["vector", "lexical", "hybrid"]
    for name, func, arg in (("search", cmd_search, "query"), ("ask", cmd_ask, "question")):
        p = sub.add_parser(name, help=f"{name} the index")
        p.add_argument(arg)
        p.add_argument("--top-k", type=int)
        p.add_argument("--mode", choices=modes)
        p.set_defaults(func=func)

    reindex = sub.add_parser("reindex", help="re-embed chunks after changing the embedding model")
    reindex.set_defaults(func=cmd_reindex)

    ev = sub.add_parser("eval", help="run the evaluation suite on a fresh temporary index")
    ev.add_argument("--corpus", default="eval/corpus")
    ev.add_argument("--dataset", default="eval/golden_v2.jsonl")
    ev.add_argument("--thresholds", help="fail (exit 1) if metrics fall outside these bounds")
    ev.add_argument("--output", help="directory for the full JSON report")
    ev.add_argument("--generation", action="store_true", help="also evaluate generated answers")
    ev.add_argument(
        "--allow-paid",
        action="store_true",
        help="allow --generation to call the paid Claude API (RAG_GENERATOR=anthropic)",
    )
    ev.add_argument(
        "--plan",
        action="store_true",
        help="preview a generation evaluation: questions, calls, cost; generates nothing",
    )
    ev.add_argument(
        "--count-tokens",
        action="store_true",
        help="with --plan: exact input tokens from the free token-counting API",
    )
    ev.add_argument("--split", choices=["dev", "test"], help="only cases in this split")
    ev.add_argument("--tags", help="only cases with any of these comma-separated tags")
    ev.add_argument(
        "--json", action="store_true", help="print the configuration and summary as JSON"
    )
    ev.add_argument("--mode", choices=modes)
    ev.add_argument("--top-k", type=int)
    ev.set_defaults(func=cmd_eval)

    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--migrate", action="store_true", help="apply migrations before starting")
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    level = settings.log_level
    if args.command != "serve" and "log_level" not in settings.model_fields_set:
        # One-shot commands print their own results; INFO logs on the terminal are noise.
        # The server, and an explicit RAG_LOG_LEVEL, keep the configured level.
        level = "WARNING"
    configure_logging(level)
    code: int = args.func(args, settings)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
