# ADR-0008: Architecture style: a local-first modular monolith behind an API and a CLI

- **Status:** Proposed, 2026-10-29. It records an evaluation the project never wrote
  down (issue #2) and proposes the style already built. The owner accepted the parts
  it rests on in [ADR-0002](0002-v0-storage-and-scope.md) (one process, SQLite, single
  tenant); accepting this ADR confirms the style as a whole.
- **Date:** 2026-10-29
- **Deciders:** Project owner (pending), with engineering analysis
- **Supersedes:** none

## Context

[ADR-0001](0001-technology-stack.md) compares technologies component by component, and
ADR-0002 compares storage options, but no record compares architecture styles. Issue #2
asks for that comparison, against named criteria, before the style is treated as decided.

The system's shape: ingest documents, retrieve passages, answer with citations, and
measure all of it reproducibly. One maintainer operates it; v0 is single tenant
(ADR-0002). Accounts, PostgreSQL and a web interface are planned (ADR-0001, Phase 5–6).

## Options considered

1. **Simple modular Python application**: a library plus a CLI, no service.
2. **Modular monolith**: one deployable, internal modules with enforced boundaries.
3. **API-based architecture**: the core behind an HTTP API that every client uses.
4. **Service-oriented architecture**: ingestion, retrieval, generation and evaluation as
   separate services.
5. **Local-first architecture**: runs on one machine, offline by default, hosted services
   optional.
6. **Hybrid**: a combination of the above.

These aren't exclusive: 2, 3 and 5 describe different aspects (deployable unit,
interface, where it runs), so the real candidates are 1, 4, and a hybrid of 2, 3 and 5.

## Evaluation

Against the criteria issue #2 names. "Evidence" points at what exists in this repository.

| Criterion | 1. Library + CLI | 4. Services | Hybrid: 2 + 3 + 5 | Evidence for the hybrid |
| --- | --- | --- | --- | --- |
| Maintainability | Good while small | Contracts between services to version and keep in step | Good: one codebase, boundaries enforced by a test | `tests/unit/test_architecture.py` |
| Complexity | Lowest | Highest: networking, retries, partial failure | Low: one process | ADR-0006 needed no broker |
| Testability | Good | Needs contract or end-to-end tests per service | Good: in-process tests plus one real-server smoke test | `make test`, `make smoke` |
| Reproducibility | Good | Harder: several versions must line up | Good: one lockfile, one version | `uv.lock`, reports record the commit |
| Local development | Good | Poor without orchestration | Good: `uv sync` and run, offline by default | README Quickstart, verified from a fresh clone |
| Extensibility | Limited: no network interface | Strong per service | Good: providers behind interfaces; the API serves any client | `Embedder`, `Generator`, `Reranker` protocols |
| Evaluation support | Good | Harder: evaluation spans services | Good: evaluation drives the same container as the API | `rag eval`, `evaluation_index` |
| Observability | Logs only | Needs distributed tracing | Good enough: request ids and per-stage timings in one process | `docs/operations.md` |
| Deployment complexity | None (nothing to deploy) | Highest | Low: one process and a database file | `docs/operations.md` |
| Performance | Fine | Network hops per query | Fine: in-memory vectors, no hops; one SQLite writer is the limit | `docs/evaluation.md`, ADR-0002 |
| Security | Single user only | More surface (service-to-service auth) | One boundary to secure; parsing isolated (ADR-0007) | `docs/security.md` |
| Portfolio value | Low: no service to show | High, but mostly infrastructure | High: the engineering is in retrieval, evaluation and reliability | — |
| Risk of over-engineering | Low | High for one maintainer and one tenant | Low | — |

## Decision (proposed)

A **local-first modular monolith with an HTTP API and a CLI** (the hybrid):

- **One process, one database file**: FastAPI and the background ingestion workers in a
  single deployable; SQLite for v0 (ADR-0002, ADR-0006).
- **Modules with enforced layering**: the API, the CLI and evaluation sit on top of the
  same `container`, which wires the core packages (`ingestion`, `retrieval`,
  `generation`, `rag`) on top of storage and parsing; a test rejects imports that cross
  layers ([architecture.md](../architecture.md#3-code-layout)).
- **API-first, with the CLI as a peer client**: both build the system through
  `container.build_container`, so they can't drift apart.
- **Local-first by default**: offline embeddings and generation, local models; the
  hosted Claude generator is opt-in (ADR-0003).

Rejected: a plain library and CLI has no service to integrate with or deploy. Separate
services multiply operational work for a single maintainer and tenant without a load or
team-size reason, and network boundaries would make evaluation and failure handling
harder.

## Consequences

### Positive

- One thing to run, test and reason about. The module boundaries keep a later split
  possible: ingestion already communicates with the rest only through the database queue
  (ADR-0006).

### Negative and risks

- Scaling is vertical and single-node until storage moves to PostgreSQL (ADR-0002).
- A heavy request still shares the process with everything else. Parsing is isolated
  (ADR-0007); embedding and reranking aren't.

## Revisit when

- Ingestion or query load needs independent scaling: extract the ingestion workers as
  their own process first, since they already talk to the service only through the
  database queue.
- Multi-tenancy arrives (Phase 5) and storage moves to PostgreSQL.
- A second team or deployment owner needs an independently released component.
