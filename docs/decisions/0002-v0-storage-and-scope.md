# ADR-0002: v0 storage and scope — SQLite, single tenant, synchronous ingestion

- **Status:** Proposed, 2026-10-29. Implemented in v0; awaiting the project
  owner's decision (status.md, decision D-3).
- **Date:** 2026-10-29
- **Deciders:** Project owner, with engineering analysis
- **Supersedes:** for v0 only, parts of [ADR-0001](0001-technology-stack.md):
  §2 (storage), §9 (background ingestion) and §10 (authentication). ADR-0001
  remains the v1 target.

## Context

[ADR-0001](0001-technology-stack.md) targets PostgreSQL + pgvector, a
Postgres-backed ingestion queue, and first-party accounts with isolation in
SQL. The first backend was built in a session that had not found ADR-0001.
The development PC has no Docker and no PostgreSQL (decision D-3 is open), so
Postgres could not be run or integration-tested there.

That session built a working v0 instead: ingestion, hybrid retrieval, grounded
generation, an HTTP API, a CLI and an evaluation gate. It used SQLite, a
single tenant and synchronous ingestion. This ADR records those departures
from ADR-0001 so the owner can accept them for v0 or direct a move to Postgres
now.

## Decision drivers

1. A runnable, tested system that needs no services to run locally or in CI
2. Keep the path to ADR-0001's target open: no decision here should be costly
   to reverse
3. Evaluation-first: add infrastructure when a requirement or a measurement
   needs it

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **SQLite + FTS5 (BM25) + exact NumPy vector search, for v0** | Zero operations: one file that is easy to back up and reproduce. FTS5 gives true BM25 with stemming, where Postgres `ts_rank_cd` does not. Exact search has no recall loss. Runs on this PC and in CI with no services. | Single writer, single node. Vectors are held in each process's memory. No row-level security. |
| PostgreSQL + pgvector now (ADR-0001 §2) | The v1 target. Concurrent writers, HNSW indexes, ownership filters in SQL. | Needs Docker or a native install on Windows (D-3), and a database service in CI. Not runnable on the development PC today. |
| SQLite + sqlite-vec | Vector tables inside SQLite. | Native extension loading varies across Python builds. Not needed at this scale. |

## Decision

Proposed for v0:

1. **Storage:** SQLite through SQLAlchemy 2, with the schema owned by Alembic
   migrations. FTS5 external-content index over chunk text, kept in sync by
   triggers. Embeddings are stored as float32 blobs and searched exactly in
   memory. An `index_state.generation` counter, bumped by triggers on every
   chunk write, lets each worker process reload its vector matrix only when
   data changes. The engine rejects non-SQLite URLs with a pointer to this ADR.
2. **Tenancy:** a single tenant. An optional static API key protects `/v1/*`.
   There are no user accounts and no per-user isolation (FR-1 and NFR-1 are not
   met).
3. **Ingestion:** synchronous, inside the upload request, in one transaction.
   A failed ingestion leaves nothing behind (NFR-5). There is no job queue and
   no `queued`/`processing`/`failed` status (FR-3 is not met).
4. **API:** synchronous FastAPI handlers, which run in the threadpool. No
   streaming (FR-9 is not met).

The data model already follows ADR-0001 where that's cheap: each chunk records
its `embedding_model`, character offsets, section path and page; duplicates are
detected by SHA-256; deletes cascade.

## Consequences

### Positive

- The whole system runs and is tested with no external services. The test
  suite and the evaluation gate run in seconds.
- Measured exact-search cost on a laptop CPU: about 0.5 ms per query at 10k
  chunks (d=384) and about 6 ms at 100k chunks, where the 100k matrix takes
  154 MB per process. That is well inside the retrieval p95 target of 300 ms.
- Lexical retrieval is true BM25.

### Negative and risks

- Moving to PostgreSQL later means a new migration history, porting the FTS5
  queries to `tsvector`, and replacing the in-memory index with pgvector. The
  retriever interface (`search(query, top_k, mode)`) and the per-chunk model id
  contain that work to the storage and retrieval modules.
- No multi-user isolation. v0 must not be exposed to mutually untrusted users.
- Large uploads block a worker for the duration of ingestion.

## Revisit when

Move to ADR-0001's PostgreSQL design when any of these holds:

- Accounts and isolation are implemented (Phase 5). Isolation belongs in SQL,
  per ADR-0001 §10.
- More than one node must serve writes, or ingestion concurrency causes
  `database is locked` errors despite WAL and the busy timeout.
- The corpus passes about 100k chunks per deployment, or vector memory per
  worker becomes a constraint.
- The owner resolves D-3 in favor of running Postgres locally.
