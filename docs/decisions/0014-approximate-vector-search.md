# ADR-0014: Approximate vector search on PostgreSQL with HNSW

- **Status:** Accepted, 2026-11-12. An engineering decision; the owner asked for the index.
- **Date:** 2026-11-12
- **Deciders:** Engineering
- **Amends:** the exact-search choice of [ADR-0013](0013-postgresql-with-pgvector.md), which
  planned this. Exact search stays the default.

## Context

On PostgreSQL, vector search compares the question with every one of the owner's chunks.
[ADR-0013](0013-postgresql-with-pgvector.md) measured retrieval at 95 ms (p95) for 10,000
chunks, and expected the 300 ms target to need an approximate index somewhere past 30,000
chunks per owner. At 100,000 chunks, measured below, exact vector search alone took 204 ms at p95.

## Decision drivers

- No deployment changes behaviour without choosing to: exact stays the default.
- The owner filter stays inside the search ([ADR-0012](0012-accounts-and-per-user-isolation.md)),
  and an owner with *k* chunks always gets *k* results.
- Quality is measured against exact search before choosing defaults, and on every run
  that uses the index: in the benchmark, and gated in CI.
- Uploads and searches continue while an index is built.

## Options considered

| Question | Options | Chosen |
| --- | --- | --- |
| Index type | IVFFlat; HNSW | **HNSW.** It needs no training on existing data, so it can be built on an empty or growing table, and it has the better speed for a given recall. IVFFlat's lists are chosen from the data and drift as it grows |
| One dimension per index | A fixed-dimension column, migrated per model; a partial expression index per model | **A partial expression index per model**, on `embedding::vector(d)` for that model's rows. The column stays dimensionless (ADR-0013), and a new model's index can be built beside the old one |
| Filtering by owner | A partial index per owner; partitions per owner; iterative index scans | **Iterative scans** (pgvector 0.8 and later) and an exact fallback. Owners are created at run time, which rules out per-owner indexes and partitions |
| Parameters | pgvector's defaults (`m` 16, `ef_construction` 64, `ef_search` 40); wider | **`m` 32, `ef_construction` 128, `ef_search` 200**, from the measurements below |
| Turning it on | Whenever an index exists; a setting | **A setting**, `RAG_VECTOR_SEARCH=approximate`: approximate results are a choice, and exact is one setting away |
| Building the index | A migration; a command | **A command**, `rag db create-vector-index`, using `CREATE INDEX CONCURRENTLY`. The index depends on the configured model, and a build can take minutes. Evaluation and the benchmark build it on their throwaway database |

## Decision

1. `RAG_VECTOR_SEARCH` is `exact` (the default) or `approximate`, which needs PostgreSQL.
   `RAG_HNSW_EF_SEARCH` (default 200, at least `RAG_CANDIDATE_POOL`) sets pgvector's
   `hnsw.ef_search` for each search.
2. `rag db create-vector-index` builds one HNSW index per embedding model:
   `CREATE INDEX CONCURRENTLY ... USING hnsw ((embedding::vector(d)) vector_ip_ops) WITH (m = 32, ef_construction = 128) WHERE embedding_model = '<model>'`.
   It refuses pgvector older than 0.8, and dimensions that don't match the model or the
   stored chunks. It rebuilds an invalid index left by a failed build. `rag db
   drop-vector-index` drops it, for any model.
3. An approximate search orders by the indexed expression under the same model condition,
   with the owner condition in the same statement, `hnsw.iterative_scan = relaxed_order`,
   and a materialized CTE whose results the outer query sorts. PostgreSQL's planner still
   chooses: for an owner with few chunks it reads just those, exactly.
4. A search that finds fewer results than it asked for is redone exactly, and counted in
   `rag_vector_exact_fallbacks_total`. That happens when the owner has fewer chunks than
   asked for, or when the scan reaches `hnsw.max_scan_tuples` before finding enough of the
   owner's.
5. `/ready` reports the index. A missing or invalid index doesn't block readiness, since
   searches are then exact; pgvector older than 0.8 does, since every approximate search
   would fail. The startup log says the same.
6. With approximate search, `rag eval` and `rag bench` build the index on their throwaway
   database. The benchmark also reports recall against exact search, which CI gates at
   10,000 chunks (`eval/bench_thresholds.approximate.json`).

## Measurements

On the laptop in [performance.md](../performance.md), with PostgreSQL 16.15 and pgvector
0.8.6 in WSL, reached over WSL's network, on libraries built by the benchmark's generator:
golden v2's nine documents and synthetic documents made of their sentences, reshuffled.
Search times are the vector search alone, over golden v2's 94 questions, *k* = 30. Recall
is the share of exact search's top 30 that the index returns, counting a result that scores
the same as the 30th.

**bge-small (384 dimensions), 38,962 chunks.** The build was stopped there: bge-small
ingests about 20 chunks a second on this CPU. Exact search took 66 ms (p50) and 77 ms (p95).

| Build | `ef_search` | p50 / p95 | Recall | Same first result | Largest drop in best score |
| --- | --- | --- | --- | --- | --- |
| `m` 16, `ef_construction` 64: 7.1 s, 76 MB | 40 | 2.4 / 2.9 ms | 0.838 | 83% | 0.024 |
| | 100 | 3.2 / 3.9 ms | 0.919 | 90% | 0.020 |
| | 200 | 4.7 / 5.8 ms | 0.954 | 95% | 0.016 |
| | 400 | 6.9 / 8.6 ms | 0.976 | 96% | 0.016 |
| `m` 32, `ef_construction` 128: 20.6 s, 79 MB | 40 | 3.3 / 4.1 ms | 0.905 | 90% | 0.025 |
| | 100 | 5.0 / 6.7 ms | 0.960 | 94% | 0.020 |
| | 200 | 6.8 / 8.9 ms | 0.980 | 97% | 0.016 |
| | 400 | 10.1 / 12.8 ms | 0.989 | 98% | 0.016 |

Counting any result within 0.01 cosine of the 30th, recall was 1.0 from `ef_search` 100 in
both builds: most of what the index misses scores almost the same as what it returns.

**What it changes end to end.** The same 94 questions through the real retriever and answer
path, on that library, counting the questions whose relevant section was among the top 5.
Synthetic documents repeat the answers' sentences under other names, so the counts are low;
what matters is the difference.

| Search | Hybrid | Vector only | Answer or abstention changed |
| --- | --- | --- | --- |
| Exact | 23 of 76 | 29 of 76 | |
| `m` 16, `ef_construction` 64, `ef_search` 200 or 400 | 22 | 27 | 0 of 94 |
| `m` 32, `ef_construction` 128, `ef_search` 100 to 400 | 23 | 28 | 0 of 94 |

The wider build matched exact search on hybrid retrieval, so it is the default, for three
times the build time. The questions the index loses are short sections of the original
documents: in a library of long synthetic chunks they sit apart, and the graph search
seldom reaches them.

**The hashing embedder (512 dimensions), 100,045 chunks.** Exact search took 179 ms (p50)
and 204 ms (p95); the 30-second build gave 260 MB. With `m` 16 and `ef_construction` 64,
`ef_search` 200 searched in 7.1 / 8.0 ms with recall 0.79; with `m` 32 and `ef_construction`
128, recall was 0.90, and 0.95 at `ef_search` 400. End to end, hybrid retrieval found the
relevant section for 15 of the 76 answerable questions, against 17 with exact search. Hashed word counts are sparse, and a short chunk's
vector shares little with the long ones: one answer ("When do monthly quotas reset?")
scored 0.42 with exact search and wasn't found at any setting. Approximate search is for
real embedding models.

**Retrieval at 100,045 chunks**, from the benchmark (`rag bench`: the hashing embedder,
hybrid retrieval, no reranker) with the defaults above, exact against approximate:

| | Exact | Approximate |
| --- | --- | --- |
| Retrieval p50 / p95 | 340 / 380 ms | 169 / 197 ms |
| HTTP at 4 concurrent clients, p95 | 728 ms | 343 ms |
| Requests per second at 4 clients | 7.6 | 16.3 |
| Index build | | 104 s, 261 MiB |
| Database | 546 MiB | 803 MiB |

Exact search fails the 300 ms target at that size; approximate search passes it. Most of
the 169 ms left is keyword search, which took 159 ms (p50) on its own.

**When PostgreSQL uses the index.** The planner compares the plans' costs. At 10,002
chunks it priced the exact plan at 2,076 and the index plan's start alone at 4,070, and
searched exactly; at 100,000 chunks it chose the index. So turning approximate search on
for a small library changes nothing until the library grows. With the index plan forced
at 10,002 chunks, as the CI gate measures it, recall was 0.991 and 0.990 in two runs, and
the first result was the same for 98.9% of questions.

**Writes.** Each new chunk is inserted into the graph. About 2,000 hashing chunks went into
the 100,000-chunk library at 214 chunks/s without an index, 127 with `m` 16 and 52 with
`m` 32. With bge-small, embedding is most of the work: about 800 chunks went into the
39,000-chunk library at 16.5 chunks/s without an index, 16.3 with `m` 16 and 15.0 with
`m` 32, 9% slower.

**Owners.** In the 100,000-chunk library, a second owner's 135 chunks were searched
exactly by the planner, through the owner's documents, in 2 to 3 ms, with or without
iterative scans. An owner with a quarter of a 5,000-chunk library is searched exactly too;
the tests force the index plan to check that iterative scans then return 30 of the owner's
chunks (recall 0.92 at `ef_search` 40).

## Consequences

- Past about 30,000 chunks per owner, vector search costs a few milliseconds instead of a
  scan: at 100,000 chunks, retrieval p95 fell from 380 to 197 ms. Keyword search (`ts_rank`
  over every match) is then the larger part of retrieval.
- Below the size where the index is cheaper, PostgreSQL keeps searching exactly, so the
  setting can be turned on before it's needed.
- Some passages exact search would find are missed, mostly ones unlike their neighbours.
  Hybrid retrieval and reranking make up for some; operators should measure with their
  own documents (`rag eval` in both modes).
- The index costs disk (about 2.1 KiB per chunk at 384 dimensions, 2.7 KiB at 512), build
  time, and work on every new chunk: 9% slower ingestion with bge-small, four times slower
  with the hashing embedder, whose embeddings cost almost nothing. A library that takes
  many writes can use `--m 16 --ef-construction 64`, with a higher `RAG_HNSW_EF_SEARCH`.
  A model change needs a new index.
- pgvector 0.8 or later is required for approximate search; exact search still works with
  0.6.

## Revisit when

- Keyword search dominates retrieval time at the libraries in use: an index-backed BM25,
  such as `pg_search`, or limiting the matches ranked.
- Recall on real documents falls short of what the evaluation needs.
- Libraries pass a million chunks: half-precision vectors (`halfvec`) or quantization, and
  the build's memory.
- pgvector's filtered search improves enough to retire the exact fallback.
