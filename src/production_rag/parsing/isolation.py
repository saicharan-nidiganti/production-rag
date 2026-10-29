"""Run document parsing in a separate, resource-limited process (ADR-0007).

Parsing untrusted files is the riskiest code the service runs: a malformed or hostile PDF can
take minutes of CPU or gigabytes of memory, or crash the parser. With
``RAG_PARSE_ISOLATION=process`` (the default), ``parse`` runs the extractor in a fresh Python
process (``worker.py``) that:

- receives the file on stdin and answers with JSON on stdout, never pickle, so whatever a
  compromised parser writes back can't execute code in the service;
- starts with an environment stripped of secrets (API keys, tokens, ``RAG_*`` settings);
- is killed after ``RAG_PARSE_TIMEOUT_S``;
- on Linux and macOS, also runs under ``RAG_PARSE_MEMORY_MB`` of address space and a CPU-time
  limit.

A parse that times out or dies fails the document with its own error instead of taking the
API process down with it. ``inline`` runs the extractor in the calling thread, for tests.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from typing import Any

from production_rag.config import Settings
from production_rag.parsing.extract import (
    ExtractedDocument,
    ExtractionError,
    Section,
    UnsupportedDocumentError,
    extract,
)

_COMMAND = [sys.executable, "-m", "production_rag.parsing.worker"]
_RAISED_AS_IS: dict[str, type[Exception]] = {
    "ExtractionError": ExtractionError,
    "UnsupportedDocumentError": UnsupportedDocumentError,
}
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


class ParseTimeoutError(RuntimeError):
    pass


class ParseFailedError(RuntimeError):
    """The parser process died, ran out of memory, or answered with something unreadable."""


def child_environment() -> dict[str, str]:
    """The parent's environment without secrets or settings: the parser needs neither."""
    return {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(("RAG_", "ANTHROPIC_"))
        and not any(marker in name.upper() for marker in _SECRET_MARKERS)
    }


def parse(filename: str, data: bytes, settings: Settings) -> ExtractedDocument:
    if settings.parse_isolation == "inline":
        return extract(filename, data, max_pdf_pages=settings.max_pdf_pages)
    header = {
        "filename": filename,
        "max_pdf_pages": settings.max_pdf_pages,
        "memory_mb": settings.parse_memory_mb,
        "cpu_s": math.ceil(settings.parse_timeout_s) + 5,
    }
    try:
        completed = subprocess.run(  # noqa: S603 - a fixed command: this package's own module
            _COMMAND,
            input=json.dumps(header).encode() + b"\n" + data,
            capture_output=True,
            timeout=settings.parse_timeout_s,
            env=child_environment(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ParseTimeoutError(
            f"parsing took longer than {settings.parse_timeout_s:g} s (RAG_PARSE_TIMEOUT_S); "
            "the file may be malformed or unusually complex"
        ) from exc
    result = _read(completed.stdout)
    if completed.returncode != 0 or result is None:
        raise ParseFailedError(
            f"the parser process stopped unexpectedly (exit code {completed.returncode}), "
            "for example by exceeding RAG_PARSE_MEMORY_MB; the file may be malformed"
        )
    if "error" in result:
        kind = result["error"].get("type")
        if kind in _RAISED_AS_IS:
            raise _RAISED_AS_IS[kind](result["error"].get("message", ""))
        if kind == "MemoryError":
            raise ParseFailedError(
                f"parsing needed more than {settings.parse_memory_mb} MB (RAG_PARSE_MEMORY_MB)"
            )
        raise ParseFailedError(f"the parser failed unexpectedly ({kind})")
    return _document(result["document"])


def _read(stdout: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(stdout)
    except ValueError:
        return None
    return value if isinstance(value, dict) and ("document" in value or "error" in value) else None


def _document(raw: dict[str, Any]) -> ExtractedDocument:
    return ExtractedDocument(
        text=raw["text"],
        title=raw["title"],
        content_type=raw["content_type"],
        sections=[Section(**section) for section in raw["sections"]],
        metadata=raw["metadata"],
    )
