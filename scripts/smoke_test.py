"""End-to-end smoke test: a real server process over HTTP, offline and free.

Starts ``rag serve`` against a temporary database with the offline configuration (hashing
embeddings, extractive generator, no API key), then checks readiness, background ingestion,
search and a cited answer. CI runs it after the test suite (``make smoke``). It exits
non-zero at the first failed check and prints the server's log.

    uv run python scripts/smoke_test.py
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = REPO_ROOT / "eval" / "corpus" / "deployment-runbook.md"
RAG = [sys.executable, "-m", "production_rag.cli"]


class SmokeTestError(AssertionError):
    pass


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeTestError(message)
    print(f"ok: {message}")


def _wait_for(condition: Callable[[], bool], what: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise SmokeTestError(f"timed out after {timeout_s:.0f} s waiting for {what}")
        time.sleep(0.2)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _stop(server: subprocess.Popen[bytes]) -> None:
    if server.poll() is not None:
        return
    if sys.platform == "win32":
        # The venv's python.exe is a launcher; end the whole process tree.
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(server.pid)], capture_output=True)
    else:
        server.send_signal(signal.SIGTERM)  # graceful: the worker hands back its document
    server.wait(timeout=30)


def run(workdir: Path) -> None:
    # Only the offline configuration: no inherited RAG_* settings, no API key, and a working
    # directory without a .env file.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("RAG_") and k not in {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
    }
    env |= {
        "RAG_DATABASE_URL": f"sqlite:///{(workdir / 'smoke.db').as_posix()}",
        "RAG_EMBEDDING_PROVIDER": "hashing",
        "RAG_RERANKER": "none",
        "RAG_GENERATOR": "extractive",
        "RAG_LOG_LEVEL": "WARNING",
    }
    subprocess.run([*RAG, "db", "upgrade"], cwd=workdir, env=env, check=True)
    print("ok: migrations applied")

    port = _free_port()
    log_path = workdir / "server.log"
    with log_path.open("wb") as log:
        server = subprocess.Popen(
            [*RAG, "serve", "--host", "127.0.0.1", "--port", str(port)],
            cwd=workdir,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            check(client, server)
    except BaseException:
        _stop(server)
        print(f"--- server log ({log_path.name}) ---", file=sys.stderr)
        print(log_path.read_text(encoding="utf-8", errors="replace")[-4000:], file=sys.stderr)
        raise
    _stop(server)
    print("ok: server stopped")


def _ready(client: httpx.Client, server: subprocess.Popen[bytes] | None) -> bool:
    if server is not None and server.poll() is not None:
        raise SmokeTestError(f"server exited with code {server.returncode}")
    try:
        return client.get("/ready").status_code == 200
    except httpx.TransportError:
        return False


def check(
    client: httpx.Client,
    server: subprocess.Popen[bytes] | None = None,
    *,
    ready_timeout_s: float = 60,
    ingest_timeout_s: float = 60,
) -> None:
    """Run every check against a server; raise SmokeTestError on the first failure.

    A response that isn't the expected shape (not JSON, a missing field) or a request that
    times out is a failure too, reported as one instead of as a traceback.
    """
    try:
        _exercise(client, server, ready_timeout_s, ingest_timeout_s)
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise SmokeTestError(f"unexpected response: {type(exc).__name__}: {exc}") from exc
    except httpx.TimeoutException as exc:
        raise SmokeTestError(f"request timed out: {exc.request.method} {exc.request.url}") from exc
    except httpx.HTTPError as exc:
        raise SmokeTestError(f"request failed: {exc}") from exc


def _exercise(
    client: httpx.Client,
    server: subprocess.Popen[bytes] | None,
    ready_timeout_s: float,
    ingest_timeout_s: float,
) -> None:
    _wait_for(lambda: _ready(client, server), "GET /ready to return 200", ready_timeout_s)
    _check(client.get("/health").json() == {"status": "ok"}, "GET /health")

    upload = client.post(
        "/v1/documents", files={"file": (DOCUMENT.name, DOCUMENT.read_bytes(), "text/markdown")}
    )
    body: dict[str, Any] = upload.json()
    _check(upload.status_code == 202, "POST /v1/documents is accepted (202)")
    document_id = body["document"]["id"]
    _check(upload.headers.get("location") == f"/v1/documents/{document_id}", "Location header")

    def processed() -> bool:
        status: str = client.get(f"/v1/documents/{document_id}").json()["status"]
        return status in {"ready", "failed"}

    _wait_for(processed, "the document to finish processing", ingest_timeout_s)
    doc = client.get(f"/v1/documents/{document_id}").json()
    _check(doc["status"] == "ready" and doc["num_chunks"] > 0, "background ingestion: ready")

    hits = client.post("/v1/search", json={"query": "rollback command", "top_k": 3}).json()
    _check(
        bool(hits["hits"]) and hits["hits"][0]["source_name"] == DOCUMENT.name,
        "POST /v1/search finds the document",
    )

    answer = client.post("/v1/query", json={"question": "How long does the canary run?"}).json()
    _check(
        answer["status"] == "answered"
        and "30 minutes" in answer["answer"]
        and bool(answer["citations"]),
        "POST /v1/query answers with a citation",
    )
    ingestion = client.get("/ready").json()["checks"]["ingestion"]
    _check(ingestion["ready"] == 1 and ingestion["workers"] == 1, "GET /ready reports ingestion")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="rag-smoke-", ignore_cleanup_errors=True) as tmp:
        try:
            run(Path(tmp))
        except (SmokeTestError, subprocess.CalledProcessError) as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
