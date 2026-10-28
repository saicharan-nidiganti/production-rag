# ADR 0002: SQLite with FTS5 and exact in-memory vector search

- **Status:** Accepted
- **Date:** 2026-10-28

## Context

We need durable storage for documents and chunks, lexical (BM25) search and vector search. The
development environment has no Docker or PostgreSQL. The methodology says to introduce
components only when they provide measurable value. Expected corpus size for this phase: up to
tens of thousands of chunks.

## Options considered

| Option | For | Against |
|---|---|---|
| **SQLite + FTS5 + NumPy brute-force cosine** | Zero operations. One file, easy to back up and reproduce. FTS5 gives BM25 with stemming. Exact search has no recall loss. | Single writer, single node. Vectors are held in process memory. |
| PostgreSQL + pgvector (+ tsvector) | Concurrent writers, HNSW indexes, one database for everything. | Needs a server everywhere, including CI. More to operate, before there is load to justify it. |
| SQLite + sqlite-vec | ANN-free vector tables inside SQLite. | Native extension loading varies across Python builds. Not needed at this scale. |
| Dedicated vector database (Qdrant, etc.) | Scale and filtering. | A second store to keep consistent, for no measured benefit yet. |

## Decision

SQLite through SQLAlchemy 2 with Alembic migrations:

- an FTS5 external-content index over chunk text, synced by triggers;
- embeddings stored as float32 blobs, searched exactly with NumPy against an in-process matrix;
- a `generation` counter, bumped by triggers on every chunk write. Each process reloads its
  matrix only when the counter changes, so multiple workers stay consistent without shared
  memory.

The engine factory rejects non-SQLite URLs with a pointer to this ADR, rather than failing in
obscure ways later.

## Consequences

Measured exact-search cost on a laptop CPU: about 0.5 ms per query at 10k chunks (d=384) and
about 6 ms at 100k chunks, with the 100k matrix taking 154 MB per process.

Revisit this decision, and move to PostgreSQL with pgvector (HNSW) and full-text search, when any
of these holds:

- the corpus passes about 100k chunks per deployment, or vector memory per worker becomes a
  constraint;
- more than one node must serve writes, or ingestion concurrency causes `database is locked`
  errors despite WAL and the busy timeout;
- metadata filtering is needed in the vector query itself.

The retriever interface (`search(query, top_k, mode)`) and the stored model id per chunk keep
that migration contained to the storage and retrieval modules.
