# ADR 0001: Backend language, framework and project layout

- **Status:** Accepted
- **Date:** 2026-10-28

## Context

The project needs an HTTP backend for ingestion, retrieval and grounded generation, plus
offline tooling for evaluation. The methodology requires reproducibility, measurable decisions
and controlled complexity. The original planning notes for this milestone were not available
when it was implemented, so this ADR records the choices that were made, to be reviewed.

## Options considered

| Option | For | Against |
|---|---|---|
| **Python + FastAPI** | The RAG, embedding and evaluation ecosystem is Python-first. Pydantic validation and an OpenAPI spec come for free. The official Anthropic SDK is first-class. | Lower raw throughput than Go or Rust; the GIL. |
| TypeScript + Node | Shares a language with any future frontend. Good SDK. | Weaker local embedding and evaluation tooling. |
| Go | Throughput, single binary. | Thin ML ecosystem; evaluation tooling would be hand-built. |

## Decision

- Python 3.12+, FastAPI, Pydantic v2, pydantic-settings for configuration, and the official
  `anthropic` SDK (2.x, API version `2026-10-21`).
- `uv` for dependency management with a committed `uv.lock`, for reproducible installs.
- `src/` layout, with a `Container` that wires components from `Settings` and is shared by the
  API and the `rag` CLI.
- Sync request handlers. Work is I/O- and CPU-bound in libraries that block, and FastAPI runs
  sync handlers in a threadpool.
- Quality gates: ruff (lint and format), mypy `--strict`, pytest with a coverage floor of 85%.

## Consequences

- Throughput per process is bounded by the threadpool. Scale out with more worker processes; the
  vector cache is consistent across processes (ADR 0002).
- An async rewrite would be needed only if the service had to hold many concurrent long-lived
  generation requests per process. Streaming responses would be the trigger to revisit.
