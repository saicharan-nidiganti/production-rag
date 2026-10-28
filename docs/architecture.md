# Architecture

This document describes the backend as implemented. The reasoning behind each major choice is
recorded in [decisions/](decisions/).

## Components

```text
                ┌──────────────┐     ┌───────────────┐
  HTTP clients ─►  FastAPI app ├─┐   │   rag CLI     │◄─ operators, CI
                └──────────────┘ │   └──────┬────────┘
                                 ▼          ▼
                        ┌──────────────────────────┐
                        │ Container (wiring only)  │
                        └─┬──────────┬──────────┬──┘
                          ▼          ▼          ▼
                   IngestionService  RagService  Evaluation runner
                          │          │  │
                          │   Retriever  Generator (Claude | extractive)
                          │    │    │
                          ▼    ▼    ▼
                   ┌────────────────────────────────────┐
                   │ SQLite: documents, chunks,         │
                   │ chunks_fts (FTS5), index_state     │
                   └────────────────────────────────────┘
```

`container.build_container(settings)` constructs every component from `Settings`. The API and
the CLI share it, so they never drift apart in how they assemble the system.

## Ingestion

`IngestionService.ingest(filename, bytes)`:

1. **Validate.** The filename is reduced to a bare name, the type must be `.txt`, `.md`/`.markdown`
   or `.pdf`, and the size must be within `RAG_MAX_UPLOAD_BYTES`.
2. **Deduplicate.** A SHA-256 of the raw bytes is looked up. Identical content returns the
   existing document (HTTP 200, `created: false`). A unique index on the hash also covers
   concurrent uploads of the same file.
3. **Extract** ([extract.py](../src/production_rag/ingestion/extract.py)) full text plus
   *sections*:
   - Markdown: one section per heading, carrying the heading path (`Guide > Install > Windows`).
     Headings inside code fences are ignored. Headings without a body of their own are skipped.
   - PDF: one section per page (pypdf), carrying the page number. Encrypted PDFs and PDFs over
     `RAG_MAX_PDF_PAGES` are rejected.
   - Text: UTF-8 (BOM tolerated) with newlines normalised. Other encodings are rejected rather
     than guessed.
4. **Chunk** ([chunking.py](../src/production_rag/ingestion/chunking.py)). Chunks never cross a
   section boundary. Each chunk is at most `RAG_CHUNK_SIZE` characters (default 1000) and ends at
   the best break in the second half of the window: a paragraph break, else a sentence end, else
   whitespace, else a hard cut. Consecutive chunks overlap by up to `RAG_CHUNK_OVERLAP`
   characters (default 150), and a chunk starts on a word boundary where possible. Offsets point
   into the document's full text, and `text[start:end] == chunk.text` is tested as an
   invariant.
5. **Embed.** The chunk text is prefixed with its heading path, which gives a short chunk its
   context.
6. **Persist** the document (including full extracted text, for re-chunking and audit) and its
   chunks in one transaction.

Raw uploaded files are never written to disk. Only extracted text is stored.

## Storage

SQLite through SQLAlchemy 2, with the schema owned by Alembic migrations
([0001_initial_schema.py](../src/production_rag/db/migrations/versions/0001_initial_schema.py)).

| Table | Purpose |
|---|---|
| `documents` | One row per unique upload: name, title, type, content hash, size, chunk count, extracted text, metadata |
| `chunks` | Text, offsets, heading path, page, embedding (float32 little-endian blob) and the embedding model id |
| `chunks_fts` | External-content FTS5 index over `chunks.text`, using the Porter stemmer |
| `index_state` | Single row with a `generation` counter |

Triggers on `chunks` keep `chunks_fts` in sync and increment `index_state.generation` on every
insert, update and delete. Foreign keys cascade document deletes to chunks, and the triggers
then clean the FTS index. Connections enable `foreign_keys`, WAL journaling and a 5 s busy
timeout.

An integration test compares the ORM models with the migrated schema, so a model change without
a migration fails CI.

## Retrieval

[retriever.py](../src/production_rag/retrieval/retriever.py) supports three modes:

- **lexical**: BM25 from FTS5. The query is reduced to content terms, each term is quoted (which
  neutralises FTS5 operators in user input), and the terms are OR-ed together.
- **vector**: exact cosine similarity against an in-memory matrix of the current embedding
  model's vectors. The matrix is reloaded only when `index_state.generation` changes, which keeps
  separate worker processes consistent without shared memory. Measured cost: about 0.5 ms per
  query at 10k chunks and 6 ms at 100k.
- **hybrid** (default): both retrievers fetch `RAG_CANDIDATE_POOL` candidates (default 30), then
  Reciprocal Rank Fusion (`k = 60`) merges them. RRF uses only ranks, so BM25 and cosine scores
  never need calibrating against each other.

Each hit carries its fused score plus its vector and lexical scores. `best_vector_score` (the top
cosine) drives abstention.

Embeddings: `hashing` is a deterministic, dependency-free signed feature-hashing model
(lexical only). `fastembed` runs a local ONNX model (default `BAAI/bge-small-en-v1.5`, 384
dimensions). Each chunk records its embedding model id. Vectors from other models are never
compared, `/ready` reports them as stale chunks, and `rag reindex` re-embeds them.

## Generation and citations

[rag.py](../src/production_rag/rag.py) orchestrates a query:

1. Retrieve `top_k` chunks.
2. **Abstain before generation** when nothing was retrieved or the best cosine is below the
   provider's calibrated threshold. This returns `insufficient_evidence` without a model call.
3. Generate.

[anthropic_generator.py](../src/production_rag/generation/anthropic_generator.py) sends each
retrieved chunk to Claude as a `search_result` content block with citations enabled, followed by
the question. The system prompt tells the model to answer only from those results, to treat
their contents as data rather than instructions, and to say plainly when they don't answer the
question. The response arrives as text blocks whose `search_result_location` citations name the
block they rely on by index, and those indexes map directly to chunks. A citation therefore can
only point at evidence that was actually retrieved: the model can't invent a source.

The answer is returned as **segments** (text plus indexes into a de-duplicated `citations`
list), which lets a UI render inline citation markers. Status rules:

| Condition | `status` |
|---|---|
| At least one citation | `answered` |
| Text but no citations (the model declined, or answered without grounding) | `insufficient_evidence` |
| `stop_reason == "refusal"` | `refused` |

Request settings: model `claude-opus-5-5`, `output_config.effort` (default `medium`), no
sampling parameters (removed in API version `2026-10-21`), and the server-side refusal fallback
(`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`). With the fallback on, a
safety-classifier false positive is retried on Anthropic's recommended model instead of failing
the request.

SDK errors are classified. Rate limits, 5xx responses and connection failures are retryable
(HTTP 503). Bad credentials, missing credentials, invalid requests and unknown models are not
(HTTP 502). The SDK retries transient failures itself (`RAG_ANTHROPIC_MAX_RETRIES`).

The **extractive** generator is an offline, deterministic baseline. It quotes the retrieved
sentences that overlap most with the question, each with its own citation. It keeps the whole
pipeline runnable and testable without credentials, but it can't synthesise or paraphrase.

## API

FastAPI with sync handlers, so blocking database and model calls run in the threadpool instead
of on the event loop. Middleware assigns each request an id (accepting a well-formed incoming
`X-Request-ID`), puts it in a context variable that every log line includes, returns it in the
response header and logs one `http.request` line with timing. Domain exceptions map to status
codes with a single error shape, and an unexpected exception becomes a 500 that keeps the
request id and hides internals.

## Observability

Logs are one JSON object per line on stderr: `ts`, `level`, `logger`, `event`, `request_id`,
plus event fields. The main events are `ingest.completed`, `query.completed` (status, mode,
counts, best vector score, whether generation was skipped, per-stage timings),
`generation.completed` (model, stop reason, token usage, Anthropic request id),
`generation.refused` and `http.request`. Question text and document content are never logged.

`/v1/query` and `/v1/search` responses also include `timings_ms`.

## Security

- Optional static API key with constant-time comparison. `/health` and `/ready` stay open for
  probes.
- Upload limits on size and PDF page count. Filenames are only metadata, never filesystem
  paths.
- FTS5 query construction neutralises operators in user input. All SQL uses bound parameters.
- Retrieved documents are untrusted input to the model. The system prompt treats them as data,
  and citations can only reference retrieved chunks. Prompt injection is mitigated, not
  eliminated: see [operations.md](operations.md#security-notes).
- Secrets are `SecretStr` and never logged.

## Known limits

- SQLite means a single writer and a single node. Vector search is exact and in memory, which is
  comfortable to about 100k chunks per process ([ADR 0002](decisions/0002-storage.md)).
- No reranker yet. Hybrid retrieval already reaches recall@5 = 1.0 on the current dataset, so a
  reranker needs a harder dataset to justify it (roadmap Phase 4).
- Ingestion is synchronous within the request. Large PDFs block a worker for the duration.
- No OCR: scanned PDFs without a text layer are rejected as having no extractable text.
