"""The parser process: ``python -m production_rag.parsing.worker`` (started by ``isolation``).

Reads one JSON header line and then the raw file from stdin, extracts it, and writes one JSON
object to stdout: ``{"document": ...}`` or ``{"error": {"type": ..., "message": ...}}``. It
imports only the standard library and the extractor, so it starts quickly and can run under
an address-space limit (numpy, for one, can't).
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from typing import Any

from production_rag.parsing.extract import ExtractionError, UnsupportedDocumentError, extract

# Errors whose message is written for the uploader; anything else reports only its type,
# because its message could contain document text.
_EXPECTED = (ExtractionError, UnsupportedDocumentError)


def _limit_resources(memory_mb: int, cpu_s: int) -> None:
    """Cap address space and CPU time where the OS supports it (not on Windows)."""
    if sys.platform == "win32":
        return  # no rlimits; the service's timeout still applies
    import resource

    memory = memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    # A backstop that holds even if the service that started this process is gone.
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))


def run(header: dict[str, Any], data: bytes) -> dict[str, Any]:
    try:
        document = extract(header["filename"], data, max_pdf_pages=header["max_pdf_pages"])
    except _EXPECTED as exc:
        return {"error": {"type": type(exc).__name__, "message": str(exc)}}
    except MemoryError:
        return {"error": {"type": "MemoryError", "message": ""}}
    except Exception as exc:  # reported by type only: the message may quote the document
        return {"error": {"type": type(exc).__name__, "message": ""}}
    return {"document": asdict(document)}


def main() -> int:
    header = json.loads(sys.stdin.buffer.readline())
    data = sys.stdin.buffer.read()
    _limit_resources(header["memory_mb"], header["cpu_s"])
    sys.stdout.write(json.dumps(run(header, data)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
