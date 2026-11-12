# ADR-0015: Keep search vectors in the row on PostgreSQL

- **Status:** Accepted, 2026-11-12. An engineering decision; the owner asked for faster
  keyword search on PostgreSQL, the next item on the roadmap.
- **Date:** 2026-11-12
- **Deciders:** Engineering

## Context

With approximate vector search ([ADR-0014](0014-approximate-vector-search.md)), keyword
search became the slower half of retrieval on PostgreSQL: about 160 ms at 100,000 chunks. It
ranks every chunk that matches any of the question's words with `ts_rank`
([ADR-0013](0013-postgresql-with-pgvector.md)).

## What the time was spent on

Measured on PostgreSQL 16.15 in WSL on the laptop in [performance.md](../performance.md),
with the benchmark's 100,045-chunk library and golden v2's 94 questions:

- **A typical question matches 65% of the chunks.** The library is built from the nine
  corpus documents' sentences, reshuffled: 100,045 chunks hold only 2,024 distinct word
  stems, and the most common appear in 70% of chunks. At that share PostgreSQL rightly
  reads the whole table instead of the full-text index: 116 ms of a 174 ms search went to
  checking every row, the rest to ranking the matches.
- **Most of that was fetching the search vector from TOAST storage.** A chunk row holds its
  text (859 bytes on average), its embedding (2 KB) and `text_search` (1,070 bytes), more
  than the roughly 2 KB PostgreSQL keeps in a row. It moves the largest values out: the
  embedding, then `text_search`, which is larger than the text. 181,439 values were out of
  line for 100,045 rows, and one search touched 340,000 buffers for a 119 MB table.

## Options considered

| Option | Result | Chosen |
| --- | --- | --- |
| Match all the question's words first, any of them only if that finds too few | 2 ms when it finds enough, but for 56 of 94 questions it found fewer than 30, so it would usually cost two searches | No |
| Parallel workers | No faster | No |
| Drop the question's most common words, using the column's statistics | Fewer matches to rank, but the results would then depend on every user's documents, the cross-tenant signal PostgreSQL's ranking otherwise avoids ([security.md](../security.md#known-gaps)) | No |
| A narrower table for searching, or other compression | Copies without the embedding were 13–19% faster at the median and no faster at p95; the vectors weren't compressed to begin with | No |
| A RUM index, which can rank inside the index | Not part of PostgreSQL or the pgvector image: an extension to build and install, and a different ranking function | Not now |
| `pg_search` (BM25 in PostgreSQL) | AGPL-3.0, outside the permissive licence policy | No |
| **Keep `text_search` in the row** (`STORAGE MAIN`), so PostgreSQL moves the text out instead | The same results; the text is read only for the chunks a search returns | **Yes** |

## Decision

1. Migration 0006 sets `ALTER TABLE chunks ALTER COLUMN text_search SET STORAGE MAIN` on
   PostgreSQL; SQLite has no such column and no TOAST. It changes only the setting, so it
   is instant, and applies to rows written from then on.
2. `rag db rebuild-search-index`, on PostgreSQL, rewrites the existing chunks in batches of
   1,000, one short transaction each, then runs `VACUUM (ANALYZE)`. Each rewritten row is
   inserted again into every index, which for an HNSW index is slow, so the command refuses
   while one exists unless given `--keep-vector-index`: drop it, rewrite, build it again.

## Measurements

Keyword search alone, over the 94 questions, before and after on the same library: the
results were identical for every question.

| | p50 | p95 |
| --- | --- | --- |
| Search vector in TOAST storage | 172 ms | 197 ms |
| In the row | 51 ms | 71 ms |

By how many chunks a one-word query matches, median of 15 runs:

| Chunks matching | Before | After |
| --- | --- | --- |
| 20 | 1.5 ms | 1.4 ms |
| 7,328 (7.3%) | 28.5 ms | 9.4 ms |
| 21,051 (21%) | 310 ms | 49 ms |
| 38,853 (39%) | 178 ms | 48 ms |
| 64,338 (64%) | 165 ms | 32 ms |

A small owner's search was already fast, because PostgreSQL starts from the owner's
documents: 2.0 ms before and 1.5 ms after, for 198 chunks among 100,045.

End to end, the benchmark (`rag bench`, the hashing embedder, hybrid retrieval) at 100,045
chunks on PostgreSQL 16.15 with pgvector 0.8.6, against the same runs for 1.1.0:

| Retrieval p50 / p95 | 1.1.0 | With this change |
| --- | --- | --- |
| Exact vector search | 340 / 380 ms | 231 / 256 ms |
| Approximate vector search | 169 / 197 ms | 71 / 85 ms |

HTTP p95 at four concurrent clients went from 728 to 507 ms with exact search, and from
343 to 126 ms with approximate search.

Rewriting the 100,045 chunks and compacting the table took 24 s. The heap grew from 119 to
140 MB, and the TOAST storage from 322 to 338 MB.

## Consequences

- Keyword search on PostgreSQL costs about a third to a sixth as much per match.
- Its cost still grows with the number of chunks a question matches. The benchmark's
  library, with its tiny vocabulary, is close to the worst case; a real library's questions
  match a smaller share.
- Deployments upgraded from 1.1.0 or earlier get the layout for new chunks at once, and for
  existing ones after `rag db rebuild-search-index`.
- Reading a chunk's text now usually means a TOAST fetch, which matters only for the few
  chunks a search returns, and for `rag reindex`, which reads every chunk.

## Revisit when

- Questions on real libraries match so many chunks that keyword search dominates again:
  then a RUM index, or a BM25 extension with a permissive licence.
- PostgreSQL changes how it chooses columns to move out of line.
