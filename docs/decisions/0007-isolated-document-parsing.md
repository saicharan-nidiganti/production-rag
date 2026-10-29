# ADR-0007: Parse documents in a separate, resource-limited process

- **Status:** Accepted, 2026-10-29. An engineering decision taken as the
  highest-priority safe improvement the owner asked for, following the trigger
  [ADR-0006](0006-background-ingestion.md) recorded ("move parsing to a
  separate, resource-limited worker").
- **Date:** 2026-10-29
- **Deciders:** Engineering
- **Supersedes:** none

## Context

Background ingestion (ADR-0006) moved parsing off the request path, but it still
ran inside the API process. A malformed or hostile file can make a parser spend
minutes of CPU, allocate gigabytes, or crash, and in-process it takes the API
down with it. The attempt limit only stopped the loop after the damage. More
parsers are coming (DOCX and HTML, FR-2), each one more attack surface.

## Decision drivers

1. A bad file must fail its own document, never the service.
2. The parser must not see secrets, and a compromised parser must not be able
   to run code in the service through what it returns.
3. No new infrastructure (ADR-0002, ADR-0006); works on Linux, macOS and
   Windows.
4. Extraction results must be identical, so evaluation results don't move.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| Keep parsing in-process, rely on the attempt limit | Nothing to build | A bad file still crashes or starves the API, three times |
| `multiprocessing` pool | Standard library; warm workers | `spawn` re-imports the parent's main module; results come back pickled; killing a stuck task means rebuilding the pool |
| **A fresh `python -m production_rag.parsing.worker` per document, JSON over stdin/stdout** ✅ | Simple; timeout by killing one process; rlimits in the child; no pickle; a minimal environment | Start-up cost per document (measured: 94 ms median on a Windows laptop) |
| Containers or a sandbox (gVisor, Firecracker, seccomp) | Strongest isolation, including network | New infrastructure and platform-specific; out of proportion for v0 |

## Decision

1. Extraction moves to a new `production_rag.parsing` package that imports only
   the standard library and pypdf. The parser process starts in about 40 ms of
   imports, against 570 ms through the ingestion package, which pulls in
   SQLAlchemy, pydantic and numpy. numpy also misbehaves under an address-space
   limit.
2. `parsing.isolation.parse` runs the extractor in a child process. The file
   goes in on stdin, and the result comes back as JSON. It is never unpickled, so
   a compromised parser can't execute code in the service.
3. The child's environment drops every `RAG_*` and `ANTHROPIC_*` variable and
   anything named like a key, token, secret, password or credential.
4. The service kills the child after `RAG_PARSE_TIMEOUT_S` (120 s). On Linux and
   macOS the child also caps its own address space at `RAG_PARSE_MEMORY_MB`
   (1024) and its CPU time at the timeout plus 5 s, a backstop that holds even if
   the service is gone.
5. A timeout fails the document with `parse_timeout`, and a crash, memory
   exhaustion or unreadable output with `parse_failed`. Extraction errors keep
   their type and message (`extraction_failed`). Unexpected exceptions report
   only their type, since their message could quote the document.
6. `RAG_PARSE_TIMEOUT_S` must be shorter than `RAG_INGEST_LEASE_S`, checked at
   startup, because parsing runs before the first lease renewal.
7. `RAG_PARSE_ISOLATION=inline` parses in-process. The test suite uses it for
   speed. Dedicated tests and the CI smoke test (a real server with the
   defaults) cover the isolated path.

Verified by tests: identical results to inline extraction for Markdown and PDF;
expected errors keep their type; timeouts, crashes, unreadable output and
unexpected errors fail cleanly; the memory limit (Linux and macOS); the stripped
environment; the setting check; and end-to-end ingestion through the parser
process, both succeeding and timing out
([test_parsing_isolation.py](../../tests/integration/test_parsing_isolation.py)).

## Consequences

### Positive

- A hostile or broken file fails its own document with a reason. The API
  process, its other documents and its queries carry on.
- The parser never holds the Claude or API keys.
- New parsers (DOCX, HTML) get the same protection for free.

### Negative and risks

- About 0.1 s more per document (94 ms median on a Windows laptop, against
  0.1 ms to parse a Markdown file in-process). That's negligible next to
  embedding, but `rag eval` and bulk `rag ingest` pay it per file.
- On Windows only the timeout applies: there are no rlimits, so a parse can
  still use a lot of memory for up to `RAG_PARSE_TIMEOUT_S`.
- The child can still reach the network and the filesystem. This is process
  isolation, not a sandbox.

## Revisit when

- Untrusted, anonymous uploads are allowed: add a real sandbox (no network,
  read-only filesystem, seccomp or a container).
- Per-document start-up cost matters for bulk ingestion: keep a small pool of
  parser processes, recycled after each failure.
