# ADR-0013: PostgreSQL with pgvector, alongside SQLite

- **Status:** Accepted, 2026-11-12. An engineering decision; the owner approved the phase
  and installed PostgreSQL for it. Its choice of exact search is amended by
  [ADR-0014](0014-approximate-vector-search.md), which adds approximate search as an option;
  exact stays the default.
- **Date:** 2026-11-12
- **Deciders:** Engineering
- **Supersedes:** the "SQLite only" scope of [ADR-0002](0002-v0-storage-and-scope.md), which
  planned this move. SQLite stays supported.

## Context

v0 ran on one SQLite file. That keeps one machine simple, but allows a single writer and
one instance, and the vector index lives in each process's memory. With accounts
([ADR-0012](0012-accounts-and-per-user-isolation.md)), the next limit is running more
than one instance against shared state. [ADR-0001](0001-technology-stack.md) §2 chose
PostgreSQL with pgvector for that.

## Decision drivers

- Existing SQLite deployments keep working, unchanged.
- One code path for everything except the two searches and file-level maintenance.
- The owner filter stays inside both searches, as ADR-0012 requires.
- Retrieval quality is measured on PostgreSQL before it is called equivalent.
- The licence policy: permissive licences only.

## Options considered

| Question | Options | Chosen |
| --- | --- | --- |
| Replace SQLite, or support both | Replace; or both, chosen by `RAG_DATABASE_URL` | **Both.** SQLite remains the zero-setup default; PostgreSQL is for several instances |
| Driver | psycopg 3 (LGPL-3.0); asyncpg (Apache-2.0, async only); pg8000 (BSD-3-Clause, pure Python) | **pg8000**, an optional extra (`postgres`). psycopg's LGPL is outside the licence policy, and the code is synchronous |
| Vector search | Load vectors into each process, as on SQLite; or search in the database | **In the database**, with pgvector's inner product. Nothing to keep in step between instances |
| Approximate index (HNSW) | Build one; or search exactly | **Exact, for now.** An approximate index with an owner filter can return fewer than *k* results for a small library, unless pgvector 0.8's iterative scans are used, and it would change measured results |
| Keyword search | `pg_trgm`; `tsvector` with `ts_rank_cd`; `tsvector` with `ts_rank` | **`tsvector` with `ts_rank`**: English stemming like SQLite's Porter tokenizer, and the same quality as `ts_rank_cd` on golden v2, about 30% faster |
| Full-text index upkeep | Triggers, as on SQLite; or a generated column | **A stored generated column** with a GIN index; the database keeps it in step |

## Decision

1. `RAG_DATABASE_URL=postgresql+pg8000://user:password@host/database` selects PostgreSQL;
   `sqlite:///path` stays the default. The `postgres` extra installs pg8000, and the
   container image includes it.
2. The migrations run on both. On PostgreSQL they create the `vector` extension, store
   embeddings as `vector` (no fixed dimension, since models differ) and add
   `chunks.text_search tsvector GENERATED ALWAYS AS (to_tsvector('english', text))` with a
   GIN index. The FTS5 table and its triggers exist only on SQLite.
3. Only two functions differ by database: lexical search and vector search. Both put the
   owner condition in the same SQL statement as the match.
4. File backups, `verify` and `restore` are SQLite tools. On PostgreSQL the commands say
   to use `pg_dump` and `pg_restore` instead.
5. Evaluation and the benchmark build their throwaway index on the same kind of database
   as the configured one, so a PostgreSQL deployment is measured on PostgreSQL.
6. The test suite runs on both: set `RAG_TEST_POSTGRES_URL`, and each test gets its own
   database. Tests of SQLite itself are marked `sqlite_only`. CI runs both, the PostgreSQL
   job against a pgvector service container.
7. The database password is redacted from logs like the API keys.

## Measurements

The offline configuration, on golden v2 (94 questions):

| Database | Hit rate | MRR | Test split hit rate | Test split MRR |
| --- | --- | --- | --- | --- |
| SQLite (FTS5 BM25) | 0.947 | 0.867 | 0.957 | 0.859 |
| PostgreSQL (`ts_rank`) | 0.961 | 0.874 | 0.957 | 0.867 |

3 of 94 questions rank their first relevant passage differently: 2 better on PostgreSQL, 1
worse. Both pass the committed retrieval gate.

`rag bench` at 10,002 chunks, on the laptop in [performance.md](../performance.md), with
PostgreSQL 16.15 and pgvector 0.6.0 in WSL, reached over WSL's network:

| | SQLite | PostgreSQL |
| --- | --- | --- |
| Retrieval p50 / p95 | 8.4 / 12.8 ms | 87 / 95 ms |
| HTTP at 4 concurrent clients | 191 requests/s | 34 requests/s |
| Ingestion, one worker | 326 chunks/s | 169 chunks/s |
| Database size | 5.5 KiB per chunk | 7.1 KiB per chunk |

PostgreSQL is slower on one machine: each search is a round trip, and both searches scan
every chunk of the owner. It stays well within the 300 ms retrieval target, and it is
what allows more than one instance.

## Consequences

- One instance on one machine is still fastest on SQLite. Choose PostgreSQL for several
  instances, managed backups, or a database the team already operates.
- Exact search grows linearly with an owner's chunks: about 95 ms at 10,000. Somewhere past
  about 30,000 chunks per owner, the 300 ms target needs an approximate index.
- With several instances, rate limits and the failed-attempt lockout are still per
  process, and the reranker and embedding models run in each.
- Both databases must pass every test, which CI enforces.

## Revisit when

- An owner's library passes about 30,000 chunks: add HNSW, as a partial expression index
  per embedding model, with pgvector ≥ 0.8 iterative scans so filtered searches still
  return *k* results; then measure recall against exact search.
- Rate limits must hold across instances: keep them in PostgreSQL or Redis.
- Keyword ranking matters more: try BM25 through an extension such as `pg_search`.
