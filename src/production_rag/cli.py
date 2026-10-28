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


def cmd_ask(args: argparse.Namespace, settings: Settings) -> int:
    from production_rag.generation import GenerationError

    container = _container(settings)
    try:
        result = container.rag.answer(args.question, top_k=args.top_k, mode=args.mode)
    except GenerationError as exc:
        print(f"generation failed: {exc}", file=sys.stderr)
        return 1
    finally:
        container.close()
    answer = result.answer
    print(f"[{answer.status} | {answer.model}]\n")
    for segment in answer.segments:
        marks = "".join(f"[{i + 1}]" for i in segment.citations)
        print(f"{segment.text}{marks}", end="")
    print("\n")
    for i, citation in enumerate(answer.citations, start=1):
        chunk = citation.chunk
        print(f"[{i}] {chunk.source_name} - {chunk.heading or '(no heading)'}")
    return 0


def cmd_reindex(_: argparse.Namespace, settings: Settings) -> int:
    container = _container(settings)
    try:
        updated = container.ingestion.reindex()
        print(json.dumps({"reembedded_chunks": updated, **container.ingestion.index_stats()}))
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
    )

    if args.output:
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"{stamp}-{report['config']['retrieval_mode']}.json"
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"report: {out_path}")

    print(json.dumps({"config": report["config"], "summary": report["summary"]}, indent=2))

    if args.thresholds:
        gate = check_gate(report, load_thresholds(Path(args.thresholds)))
        for line in gate.checks:
            print(f"PASS {line}")
        for line in gate.failures:
            print(f"FAIL {line}")
        print("gate: passed" if gate.passed else "gate: FAILED")
        return 0 if gate.passed else 1
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
    configure_logging(settings.log_level)
    code: int = args.func(args, settings)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
