"""`rag bench`: a reproducible performance benchmark (docs/performance.md).

It runs the configured pipeline on a temporary database, never the configured one:

1. **Ingestion**: the evaluation corpus, then seeded synthetic documents made from its
   sentences until the index holds the requested number of chunks (10,000 by default, the
   reference size in requirements §7). One document at a time, as one worker does.
2. **PDF**: one generated text PDF of ``pdf_pages`` pages, end to end.
3. **Questions**: the question set, in this process, ``repeat`` times after ``warmup``
   questions that aren't recorded (they load the vector matrix and warm caches). Each
   question's stage timings come from the pipeline's own timer.
4. **Streams**: ``stream_samples`` streamed answers, for the time to the first token.
5. **HTTP**: a real ``rag serve`` process on the same database, with ``concurrency``
   clients sending ``http_requests`` questions after a warm-up, rate limits off. End-to-end
   latency and throughput, and the server's CPU and memory where its metrics expose them.

The report records the environment, configuration and workload, so a run can be repeated
and two runs compared; ``perfgate`` turns it into a pass, warn or fail.
"""

from __future__ import annotations

import json
import os
import platform
import random
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from prometheus_client.parser import text_string_to_metric_families
from pydantic import SecretStr
from sqlalchemy import text

from production_rag import __version__
from production_rag.config import Settings
from production_rag.container import Container, build_container
from production_rag.db.scratch import scratch_database
from production_rag.db.vector_index import index_name
from production_rag.evaluation.dataset import EvalCase, load_dataset
from production_rag.evaluation.perfgate import summarize
from production_rag.evaluation.plan import PRICES_PER_MTOK
from production_rag.evaluation.runner import _git_commit, _sha256_file, corpus_fingerprint
from production_rag.rag import QueryDone

BENCHMARK_VERSION = 1
CORPUS_GENERATOR_VERSION = 1
_DOCUMENT_CHARS = 48_000  # about 50 chunks at the default chunk size
_SECTION_CHARS = 4_000


class BenchmarkError(RuntimeError):
    """The benchmark couldn't run; distinct from a run whose numbers fail the gate."""


@dataclass(frozen=True)
class BenchOptions:
    corpus_dir: Path
    dataset: Path
    chunks: int = 10_000
    seed: int = 7
    repeat: int = 3
    warmup: int = 10
    stream_samples: int = 20
    pdf_pages: int = 100
    http_requests: int = 200
    concurrency: int = 4

    def model_calls(self, questions: int) -> int:
        """Generator calls the run makes, at most: what a paid run would be billed for."""
        http = self.http_requests + 2 * self.concurrency if self.http_requests else 0
        return self.warmup + self.repeat * questions + self.stream_samples + http


Progress = Callable[[str], None]


def run(
    settings: Settings, options: BenchOptions, progress: Progress = lambda _: None
) -> dict[str, Any]:
    cases = load_dataset(options.dataset)
    corpus_files = sorted(options.corpus_dir.glob("*.md"))
    if not corpus_files:
        raise BenchmarkError(f"no Markdown files in {options.corpus_dir}")
    if not cases:
        raise BenchmarkError(f"no questions in {options.dataset}")

    with (
        tempfile.TemporaryDirectory(prefix="rag-bench-", ignore_cleanup_errors=True) as tmp,
        scratch_database(settings.database_url) as url,
    ):
        bench = settings.model_copy(update={"database_url": url, "ingest_workers": 0})
        container = build_container(bench)
        config = _config(settings, container)
        cpu_start = time.process_time()
        try:
            progress(f"ingesting to {options.chunks:,} chunks")
            ingestion = _ingest(container, corpus_files, options)
            pdf = _pdf(container, options) if options.pdf_pages else None
            index = container.build_vector_index()
            if index is not None:
                progress("built the HNSW index")
                ingestion["vector_index"] = {
                    key: index.get(key)
                    for key in ("seconds", "size_bytes", "dimensions", "m", "ef_construction")
                }
            ingestion.update(_database_size(container, ingestion["chunks"]))
            progress(f"answering {len(cases)} questions x {options.repeat}")
            query, query_rows, tokens = _questions(container, cases, options)
            if index is not None:
                query["vector_recall"] = vector_recall(container, cases, settings.candidate_pool)
            streams = _streams(container, cases, options)
        finally:
            container.close()
        resources = {
            "peak_rss_bytes": peak_rss_bytes(),
            "cpu_seconds": round(time.process_time() - cpu_start, 3),
        }
        http: dict[str, Any] | None = None
        http_rows: list[float] = []
        if options.http_requests:
            progress(f"HTTP: {options.http_requests} requests at concurrency {options.concurrency}")
            http, http_rows = _http(bench, cases, options, Path(tmp))

    return {
        "benchmark_version": BENCHMARK_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "environment": environment(),
        "config": config,
        "workload": _workload(options, cases, corpus_files),
        "ingestion": ingestion,
        "pdf": pdf,
        "query": query,
        "stream": streams,
        "http": http,
        "resources": resources,
        "tokens": tokens,
        "measurements": {
            "query": query_rows,
            "first_token_ms": streams.get("first_token_ms_values", []),
            "http_ms": http_rows,
        },
    }


# --- Workload ---


def corpus_sentences(files: Sequence[Path]) -> list[str]:
    """Every sentence of the corpus's prose, in a stable order."""
    sentences: list[str] = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip().lstrip("-*0123456789. ").strip()
            if not line or line.startswith(("#", "|", "```", ">")):
                continue
            sentences.extend(s.strip() + "." for s in re.split(r"(?<=\.)\s+", line) if len(s) > 20)
    return [s.replace("..", ".") for s in sentences]


def synthetic_documents(sentences: Sequence[str], seed: int) -> Iterator[tuple[str, bytes]]:
    """Documents of about 48,000 characters, with a heading every 4,000, built from the
    corpus's sentences in a seeded order. The same seed always yields the same bytes."""
    if not sentences:
        raise BenchmarkError("the corpus has no sentences to build documents from")
    rng = random.Random(seed)  # noqa: S311 - a reproducible workload, not a secret
    words = sorted({w.lower() for s in sentences for w in re.findall(r"[A-Za-z]{5,}", s)})
    number = 0
    while True:
        number += 1
        parts = [f"# Benchmark document {number}\n"]
        size = 0
        while size < _DOCUMENT_CHARS:
            heading = f"\n## {rng.choice(words).capitalize()} {rng.choice(words)} {number}\n\n"
            body = " ".join(rng.choice(sentences) for _ in range(_SECTION_CHARS // 90))
            parts += [heading, body, "\n"]
            size += len(heading) + len(body)
        yield f"bench-{number:05d}.md", "".join(parts).encode()


def text_pdf(pages: int, sentences: Sequence[str], seed: int = 7, lines: int = 40) -> bytes:
    """A valid PDF with ``lines`` lines of Helvetica text on each page."""
    rng = random.Random(seed)  # noqa: S311 - a reproducible workload, not a secret
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(pages))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    font = 3 + 2 * pages
    for i in range(pages):
        text = [rng.choice(sentences)[:90] for _ in range(lines)]
        escaped = [t.encode("ascii", "replace").decode().replace("\\", "").replace("(", "")
                   .replace(")", "") for t in text]  # fmt: skip
        stream = ("BT /F1 10 Tf 13 TL 54 750 Td " + " ".join(f"({t}) Tj T*" for t in escaped)
                  + " ET").encode()  # fmt: skip
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


# --- Phases ---


def _ingest(container: Container, files: Sequence[Path], options: BenchOptions) -> dict[str, Any]:
    documents = chunks = characters = 0
    start = time.perf_counter()

    def add(name: str, data: bytes) -> None:
        nonlocal documents, chunks, characters
        result = container.ingestion.ingest(name, data)
        documents += 1
        chunks += result.document.num_chunks
        characters += len(result.document.text)

    for path in files:
        add(path.name, path.read_bytes())
    generated = synthetic_documents(corpus_sentences(files), options.seed)
    while chunks < options.chunks:
        add(*next(generated))
    seconds = time.perf_counter() - start
    return {
        "documents": documents,
        "chunks": chunks,
        "characters": characters,
        "seconds": round(seconds, 3),
        "documents_per_s": round(documents / seconds, 2),
        "chunks_per_s": round(chunks / seconds, 1),
    }


def _pdf(container: Container, options: BenchOptions) -> dict[str, Any]:
    files = sorted(options.corpus_dir.glob("*.md"))
    data = text_pdf(options.pdf_pages, corpus_sentences(files), options.seed)
    start = time.perf_counter()
    result = container.ingestion.ingest("bench-text.pdf", data)
    seconds = time.perf_counter() - start
    return {
        "pages": options.pdf_pages,
        "size_bytes": len(data),
        "chunks": result.document.num_chunks,
        "seconds": round(seconds, 3),
        "pages_per_s": round(options.pdf_pages / seconds, 1),
    }


def _database_size(container: Container, chunks: int) -> dict[str, Any]:
    if container.engine.dialect.name == "postgresql":
        with container.engine.connect() as pg:
            size = int(pg.execute(text("SELECT pg_database_size(current_database())")).scalar_one())
        return {"database_bytes": size, "bytes_per_chunk": round(size / max(chunks, 1))}
    path = Path(str(container.engine.url.database))
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    wal = path.with_name(path.name + "-wal")
    size = path.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    return {"database_bytes": size, "bytes_per_chunk": round(size / max(chunks, 1))}


def vector_recall(container: Container, cases: Sequence[EvalCase], k: int) -> dict[str, Any]:
    """How closely the HNSW index matches exact search on the dataset's questions: the share
    of the exact top ``k`` it returns (a result scoring the same as the exact k-th counts,
    since ties may come in either order), how often its first result is the exact first, and
    the largest shortfall in the best score, which drives abstention.

    PostgreSQL uses the index only where its cost model says it is cheaper, which in a small
    library it isn't: compared as the planner chooses, the search would often be exact
    against itself. So the planner's own choice is reported (``planner_used_index_rate``),
    and the comparison runs with in-query sorts made to look expensive, which only an exact
    plan needs; ``index_used_rate`` confirms the index was read."""
    index = container.rag.retriever.vector_index
    name = index_name(container.embedder.model_id)

    def through_index(plan: list[str]) -> bool:
        return any(f"Index Scan using {name}" in line for line in plan)

    queries = [container.embedder.embed_query(case.question) for case in cases]
    exact_results: list[tuple[Any, list[tuple[int, float]]]] = []
    planner_chose = 0
    with container.sessions() as session:
        for query in queries:
            exact = index.search(session, query, k, exact=True)
            if exact:
                exact_results.append((query, exact))
                planner_chose += through_index(index.approximate_plan(session, query, k))
    if not exact_results:
        return {"k": k, "questions": 0}
    recalls: list[float] = []
    gaps: list[float] = []
    same_first = read_index = 0
    with container.sessions() as session:
        session.execute(text("SET LOCAL enable_sort = off"))  # this transaction only
        for query, exact in exact_results:
            read_index += through_index(index.approximate_plan(session, query, k))
            approximate = index.search(session, query, k)
            kth = exact[-1][1]
            found = sum(score >= kth - _TIE for _, score in approximate)
            recalls.append(min(found, len(exact)) / len(exact))
            gap = exact[0][1] - (approximate[0][1] if approximate else 0.0)
            gaps.append(max(gap, 0.0))
            same_first += gap <= _TIE
    measured = len(recalls)
    return {
        "k": k,
        "questions": measured,
        "recall_mean": round(sum(recalls) / measured, 4),
        "recall_min": round(min(recalls), 4),
        "same_first_rate": round(same_first / measured, 4),
        "best_score_gap_max": round(max(gaps), 4),
        "index_used_rate": round(read_index / measured, 4),
        "planner_used_index_rate": round(planner_chose / measured, 4),
    }


_TIE = 1e-6  # scores are float32 cosines


def _questions(
    container: Container, cases: Sequence[EvalCase], options: BenchOptions
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    for i in range(options.warmup):
        container.rag.answer(cases[i % len(cases)].question)
    rows: list[dict[str, Any]] = []
    stages: dict[str, list[float]] = {}
    statuses: Counter[str] = Counter()
    abstained = 0
    input_tokens = output_tokens = 0
    models: set[str] = set()
    cpu = time.process_time()
    start = time.perf_counter()
    for iteration in range(options.repeat):
        for case in cases:
            result = container.rag.answer(case.question)
            for stage, ms in result.timings_ms.items():
                stages.setdefault(stage, []).append(ms)
            statuses[result.answer.status] += 1
            abstained += result.abstained_before_generation
            if result.answer.usage:
                input_tokens += result.answer.usage.get("input_tokens", 0)
                output_tokens += result.answer.usage.get("output_tokens", 0)
                models.add(result.answer.model)
            rows.append({
                "id": case.id,
                "iteration": iteration,
                "status": result.answer.status,
                "timings_ms": {k: round(v, 3) for k, v in result.timings_ms.items()},
            })  # fmt: skip
    seconds = time.perf_counter() - start
    measured = len(rows)
    query = {
        "questions": len(cases),
        "measured": measured,
        "latency_ms": {stage: summarize(values) for stage, values in sorted(stages.items())},
        "queries_per_s": round(measured / seconds, 1),
        "cpu_ms_per_query": round((time.process_time() - cpu) * 1000 / measured, 3),
        "statuses": dict(sorted(statuses.items())),
        "abstained_before_generation": abstained,
    }
    return query, rows, _tokens(input_tokens, output_tokens, models, measured)


def _tokens(input_tokens: int, output_tokens: int, models: set[str], calls: int) -> dict[str, Any]:
    if not models:
        return {"available": False, "reason": "the generator reports no token usage"}
    total = input_tokens + output_tokens
    tokens: dict[str, Any] = {
        "available": True,
        "input": input_tokens,
        "output": output_tokens,
        "total": total,
        "per_query": round(total / calls, 1),
        "estimated_cost_usd": None,
    }
    prices = [PRICES_PER_MTOK.get(model) for model in models]
    if len(models) == 1 and prices[0] is not None:
        price_in, price_out = prices[0]
        cost = input_tokens * price_in / 1e6 + output_tokens * price_out / 1e6
        tokens["estimated_cost_usd"] = round(cost, 4)
        tokens["price_per_mtok"] = {"input": price_in, "output": price_out}
    return tokens


def _streams(
    container: Container, cases: Sequence[EvalCase], options: BenchOptions
) -> dict[str, Any]:
    first: list[float] = []
    for i in range(options.stream_samples):
        for event in container.rag.stream_answer(cases[i % len(cases)].question):
            if isinstance(event, QueryDone) and "first_token" in event.result.timings_ms:
                first.append(event.result.timings_ms["first_token"])
    return {
        "samples": options.stream_samples,
        "answered": len(first),
        "first_token_ms": summarize(first),
        "first_token_ms_values": [round(v, 3) for v in first],
    }


def _http(
    settings: Settings, cases: Sequence[EvalCase], options: BenchOptions, workdir: Path
) -> tuple[dict[str, Any], list[float]]:
    port = _free_port()
    log_path = workdir / "server.log"
    with log_path.open("wb") as log:
        server = subprocess.Popen(  # noqa: S603 - this interpreter, fixed arguments
            [sys.executable, "-m", "production_rag.cli", "serve", "--port", str(port)],
            env=server_environment(settings),
            cwd=workdir,  # no .env there: the environment alone decides the configuration
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_until_ready(base, server, log_path)
        before = _server_resources(base)
        questions = [case.question for case in cases]
        _load(base, questions, 2 * options.concurrency, options.concurrency)  # warm-up
        start = time.perf_counter()
        latencies, errors = _load(base, questions, options.http_requests, options.concurrency)
        seconds = time.perf_counter() - start
        after = _server_resources(base)
    finally:
        _stop(server)
    completed = len(latencies)
    http: dict[str, Any] = {
        "requests": options.http_requests,
        "concurrency": options.concurrency,
        "errors": errors,
        "seconds": round(seconds, 3),
        "requests_per_s": round(completed / seconds, 1),
        "latency_ms": summarize(latencies),
        "server": _server_usage(before, after, completed),
    }
    return http, [round(v, 3) for v in latencies]


def _load(
    base: str, questions: Sequence[str], total: int, concurrency: int
) -> tuple[list[float], int]:
    lock = threading.Lock()
    counter = iter(range(total))
    latencies: list[float] = []
    errors = 0

    def client_loop() -> None:
        nonlocal errors
        with httpx.Client(base_url=base, timeout=120) as client:
            while True:
                with lock:
                    i = next(counter, None)
                if i is None:
                    return
                start = time.perf_counter()
                try:
                    ok = (
                        client.post(
                            "/v1/query", json={"question": questions[i % len(questions)]}
                        ).status_code
                        == 200
                    )
                except httpx.HTTPError:
                    ok = False
                elapsed = (time.perf_counter() - start) * 1000
                with lock:
                    if ok:
                        latencies.append(elapsed)
                    else:
                        errors += 1

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for future in [pool.submit(client_loop) for _ in range(concurrency)]:
            future.result()
    return latencies, errors


def server_environment(settings: Settings) -> dict[str, str]:
    """The environment for a ``rag serve`` that runs exactly ``settings``: every setting
    as its ``RAG_`` variable, rate limits off, no API key, no ingestion workers."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("RAG_")}
    for name, value in settings.model_dump().items():
        if value is None or isinstance(value, SecretStr):
            continue
        if name == "fastembed_cache_dir":  # the server runs elsewhere; keep the same cache
            value = str(Path(value).resolve())
        env[f"RAG_{name.upper()}"] = str(value).lower() if isinstance(value, bool) else str(value)
    for operation in ("UPLOAD", "QUERY", "SEARCH", "READ", "AUTH_FAILURES"):
        env[f"RAG_RATE_LIMIT_{operation}_PER_MINUTE"] = "0"
    env |= {"RAG_API_KEY": "", "RAG_INGEST_WORKERS": "0", "RAG_LOG_LEVEL": "WARNING"}
    env["RAG_METRICS_ENABLED"] = "true"
    return env


def _wait_until_ready(base: str, server: subprocess.Popen[bytes], log_path: Path) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if server.poll() is not None:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise BenchmarkError(f"the server exited with {server.returncode}:\n{tail}")
        try:
            if httpx.get(f"{base}/ready", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise BenchmarkError("the server didn't become ready within 60 s")


def _server_resources(base: str) -> dict[str, float]:
    try:
        text = httpx.get(f"{base}/metrics", timeout=10).text
    except httpx.HTTPError:
        return {}
    wanted = {"process_cpu_seconds_total", "process_resident_memory_bytes"}
    return {
        sample.name: sample.value
        for family in text_string_to_metric_families(text)
        for sample in family.samples
        if sample.name in wanted
    }


def _server_usage(
    before: Mapping[str, float], after: Mapping[str, float], requests: int
) -> dict[str, Any]:
    cpu = "process_cpu_seconds_total"
    if cpu not in after or cpu not in before:
        return {
            "available": False,
            "reason": "the server's process metrics need Linux (they read /proc)",
        }
    return {
        "available": True,
        "cpu_ms_per_request": round((after[cpu] - before[cpu]) * 1000 / max(requests, 1), 3),
        "rss_bytes": int(after["process_resident_memory_bytes"]),
    }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stop(server: subprocess.Popen[bytes]) -> None:
    if server.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(server.pid)], capture_output=True)  # noqa: S603, S607
    else:
        server.send_signal(signal.SIGTERM)
    try:
        server.wait(timeout=30)
    except subprocess.TimeoutExpired:
        server.kill()


# --- Metadata ---


def peak_rss_bytes() -> int | None:
    """This process's peak resident memory, or None where it can't be read."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        current = ctypes.windll.kernel32.GetCurrentProcess
        current.restype = wintypes.HANDLE  # a 64-bit pseudo-handle, not a C int
        memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        memory_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        memory_info.restype = wintypes.BOOL
        if not memory_info(current(), ctypes.byref(counters), counters.cb):
            return None
        return int(counters.PeakWorkingSetSize)
    else:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak) if sys.platform == "darwin" else int(peak) * 1024  # macOS: bytes


def environment() -> dict[str, Any]:
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "cpu_count": os.cpu_count(),
        "python": ".".join(platform.python_version_tuple()[:2]),
        "python_full": platform.python_version(),
        "sqlite_version": sqlite3.sqlite_version,
        "version": __version__,
        "git_commit": _git_commit(),
    }


def _config(settings: Settings, container: Container) -> dict[str, Any]:
    return {
        "database": container.engine.dialect.name,
        "embedding_model": container.embedder.model_id,
        "reranker": settings.reranker,
        "rerank_candidates": settings.rerank_candidates if settings.reranker != "none" else None,
        "generator": container.rag.generator.model_id,
        "retrieval_mode": settings.retrieval_mode,
        "top_k": settings.top_k,
        "candidate_pool": settings.candidate_pool,
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "parse_isolation": settings.parse_isolation,
        "vector_search": settings.vector_search,
        "hnsw_ef_search": (
            settings.hnsw_ef_search if settings.vector_search == "approximate" else None
        ),
    }


def _workload(
    options: BenchOptions, cases: Sequence[EvalCase], files: Sequence[Path]
) -> dict[str, Any]:
    tags: Counter[str] = Counter(tag for case in cases for tag in case.tags)
    settings = asdict(options)
    for key in ("corpus_dir", "dataset"):
        settings.pop(key)
    return {
        "corpus": {
            "name": options.corpus_dir.name,
            "fingerprint": corpus_fingerprint(list(files), options.corpus_dir),
            "generator_version": CORPUS_GENERATOR_VERSION,
        },
        "dataset": {
            "name": options.dataset.name,
            "sha256": _sha256_file(options.dataset),
            "questions": len(cases),
            "answerable": sum(case.answerable for case in cases),
            "categories": dict(sorted(tags.items())),
        },
        **settings,
    }


# --- Output ---


def without_measurements(report: Mapping[str, Any]) -> dict[str, Any]:
    """The report as a baseline: everything but the individual measurements."""
    baseline = {k: v for k, v in report.items() if k not in ("measurements", "gate")}
    if isinstance(baseline.get("stream"), dict):
        baseline["stream"] = {
            k: v for k, v in baseline["stream"].items() if k != "first_token_ms_values"
        }
    return baseline


def write(report: Mapping[str, Any], directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    data, summary = directory / "benchmark.json", directory / "benchmark.md"
    data.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    summary.write_text(render(report), encoding="utf-8")
    return data, summary


def render(report: Mapping[str, Any]) -> str:
    """A Markdown summary of a report and its gate."""
    env, config, work = report["environment"], report["config"], report["workload"]
    gate = report.get("gate", {})
    lines = [
        f"# Benchmark, {report['created_at'][:19].replace('T', ' ')} UTC",
        "",
        f"**Gate: {str(gate.get('outcome', 'not evaluated')).upper()}**",
        "",
        f"- Environment: {env['os']} {env['os_release']} {env['machine']}, "
        f"{env['cpu_count']} CPUs, Python {env['python_full']}, SQLite {env['sqlite_version']}; "
        f"production-rag {env['version']}"
        + (f" ({env['git_commit'][:7]})" if env.get("git_commit") else ""),
        f"- Configuration: {config['embedding_model']}, reranker {config['reranker']}, "
        f"{config['generator']}, {config['retrieval_mode']} retrieval, top {config['top_k']}"
        + (
            f", approximate vector search (ef_search {config['hnsw_ef_search']})"
            if config.get("vector_search") == "approximate"
            else ""
        ),
        f"- Workload: {report['ingestion']['chunks']:,} chunks in "
        f"{report['ingestion']['documents']} documents; {work['dataset']['questions']} questions "
        f"x {work['repeat']} after {work['warmup']} warm-up",
        "",
        "| Latency (ms) | p50 | p95 | p99 | max |",
        "| --- | --- | --- | --- | --- |",
    ]
    rows = [(f"Question: {stage}", stats) for stage, stats in report["query"]["latency_ms"].items()]
    rows.append(("Streamed: first token", report["stream"]["first_token_ms"]))
    if report.get("http"):
        rows.append(
            (f"HTTP, {report['http']['concurrency']} concurrent", report["http"]["latency_ms"])
        )
    for label, stats in rows:
        if stats.get("count"):
            lines.append(
                f"| {label} | {stats['p50']} | {stats['p95']} | {stats['p99']} | {stats['max']} |"
            )
    ingestion, query = report["ingestion"], report["query"]
    lines += [
        "",
        "| Throughput and resources | |",
        "| --- | --- |",
        f"| Ingestion, one worker | {ingestion['documents_per_s']} documents/s, "
        f"{ingestion['chunks_per_s']} chunks/s |",
    ]
    if report.get("pdf"):
        pdf = report["pdf"]
        lines.append(
            f"| {pdf['pages']}-page text PDF | {pdf['seconds']} s, {pdf['chunks']} chunks |"
        )
    lines.append(f"| Questions, one thread | {query['queries_per_s']}/s |")
    if ingestion.get("vector_index"):
        built = ingestion["vector_index"]
        lines.append(
            f"| HNSW index build | {built['seconds']} s, {built['size_bytes'] / 2**20:.1f} MiB "
            f"(m {built['m']}, ef_construction {built['ef_construction']}) |"
        )
    if query.get("vector_recall", {}).get("questions"):
        recall = query["vector_recall"]
        lines.append(
            f"| Approximate against exact, top {recall['k']} | recall {recall['recall_mean']} "
            f"(lowest {recall['recall_min']}), same first result {recall['same_first_rate']}, "
            f"best score short by at most {recall['best_score_gap_max']} (the index read in "
            f"{recall['index_used_rate']:.0%} of these searches) |"
        )
        lines.append(
            f"| Searches the planner sent through the index | "
            f"{recall['planner_used_index_rate']:.0%} |"
        )
    if report.get("http"):
        http = report["http"]
        lines.append(
            f"| HTTP, {http['concurrency']} concurrent | {http['requests_per_s']} requests/s, "
            f"{http['errors']} errors |"
        )
        server = http["server"]
        lines.append(
            f"| Server CPU per request | {server['cpu_ms_per_request']} ms |"
            if server.get("available")
            else f"| Server CPU and memory | unavailable: {server['reason']} |"
        )
    lines.append(f"| CPU per question, in process | {query['cpu_ms_per_query']} ms |")
    peak = report["resources"]["peak_rss_bytes"]
    lines.append(
        f"| Peak memory, benchmark process | {peak / 2**20:.0f} MiB |"
        if peak
        else "| Peak memory | unavailable |"
    )
    lines.append(
        f"| Database | {ingestion['database_bytes'] / 2**20:.1f} MiB, "
        f"{ingestion['bytes_per_chunk'] / 1024:.1f} KiB per chunk |"
    )
    tokens = report["tokens"]
    if tokens.get("available"):
        cost = tokens.get("estimated_cost_usd")
        lines.append(f"| Tokens | {tokens['total']:,} ({tokens['per_query']} per question)"
                     + (f", about ${cost}" if cost is not None else "") + " |")  # fmt: skip
    else:
        lines.append(f"| Tokens | unavailable: {tokens['reason']} |")
    if gate.get("checks") or gate.get("notes"):
        lines += ["", "| Check | Result | Detail |", "| --- | --- | --- |"]
        lines += [f"| `{c['metric']}` | {c['outcome']} | {c['detail']} |" for c in gate["checks"]]
        lines += ["", *[f"- {note}" for note in gate.get("notes", [])]]
    return "\n".join(lines).rstrip() + "\n"
