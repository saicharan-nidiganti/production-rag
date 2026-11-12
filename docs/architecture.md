# Architecture

This document describes the **v0 backend as implemented**, then the path to the
v1 target defined in [ADR-0001](decisions/0001-technology-stack.md) and
[requirements.md](requirements.md). The reasoning behind each choice is in
[decisions/](decisions/).

## 1. Principles

1. **Evidence first.** Every answer can be traced back to stored chunks. A
   citation that does not map to retrieved evidence is a bug.
2. **Documents are untrusted input.** They can contain malicious files or
   prompt-injection text, and are handled as data, never as instructions.
3. **Swappable, measurable stages.** Extraction, chunking, embedding,
   retrieval and generation sit behind interfaces, so each can be evaluated
   and replaced on its own.
4. **Few moving parts.** v0 is one process type and one database file.
5. **Isolation at the data layer.** With accounts on, the owner is part of every
   query that reads documents, chunks or vectors, inside both searches
   ([ADR-0012](decisions/0012-accounts-and-per-user-isolation.md)). With them off,
   there is one shared space.

## 2. Components (v0)

```mermaid
flowchart LR
    http([HTTP client]) --> guard["RequestGuard<br/>key, rate limits, body size"] --> routes["FastAPI routes<br/>JSON and SSE"]
    op([Operator or CI]) --> cli["rag CLI"]
    evalr["Evaluation runner"]
    routes --> wiring
    cli --> wiring
    evalr --> wiring
    wiring["build_container<br/>component wiring"] --> ingestion["IngestionService<br/>and worker threads"]
    wiring --> rag["RagService"]
    ingestion -- "stdin, JSON" --> parser[["Parser process"]]
    rag --> retriever["Retriever"]
    rag --> generator["Generator<br/>Claude or extractive"]
    retriever --> reranker["Reranker<br/>cross-encoder, optional"]
    ingestion --> embedder["Embedder<br/>hashing or fastembed"]
    retriever --> embedder
    ingestion --> db[("SQLite")]
    retriever --> db
    generator -- "HTTPS, streamed" --> claude{{"Claude API"}}
    classDef external fill:#fff4e5,stroke:#c05621,color:#1a202c
    classDef store fill:#e6f0ff,stroke:#2b6cb0,color:#1a202c
    classDef isolated fill:#f7fafc,stroke:#4a5568,stroke-dasharray:5 3,color:#1a202c
    class claude external
    class db store
    class parser isolated
```

`container.build_container(settings)` constructs every component from
`Settings`. The API, the CLI and the evaluation runner all use it, so they
can't drift apart in how they assemble the system. In front of the routes, the
`RequestGuard` middleware checks the API key, the client's rate limit and the
body size before a request body is read. The parser process is the only other
process: one per upload, started by the ingestion worker. How the pieces are
deployed is in [operations.md](operations.md#reference-deployment).

## 3. Code layout

```text
src/production_rag/
  config.py          Settings (RAG_* environment variables, .env)
  observability.py   JSON logging, request-id context, stage timer
  text.py            tokenization shared by embeddings, lexical search, evaluation
  db/                SQLAlchemy models, engine (SQLite or PostgreSQL), Alembic migrations (single
                     source of schema truth), scratch databases for evaluation,
                     document queries (documents.py), backups, verification and restores
                     (maintenance.py)
  embeddings.py      Embedder protocol: hashing and fastembed providers
  metrics.py         Prometheus counters and histograms, updated where events are logged
  parsing/           text extraction (text, Markdown, PDF, DOCX, HTML) and the isolated
                     parser process that runs it
  ingestion/         chunking, the queue's claim protocol (queue.py),
                     processing (pipeline.py), worker threads
  retrieval/         vector index, FTS5 search, RRF fusion, cross-encoder reranking, retriever
  generation/        Generator protocol: Claude and extractive generators, answer types
  accounts.py        users, Argon2id passwords, sessions, personal API tokens
  rag.py             query orchestration and abstention
  container.py       component wiring
  api/               FastAPI app, request guard (API key, rate limits, body size), routes,
                     Server-Sent Events, /metrics, the web interface's files, schemas,
                     error mapping (transport only)
  cli.py             rag command (transport only)
  evaluation/        golden dataset, metrics, runner, regression gate; the performance
                     benchmark and its gate
web/                 the web interface (React, TypeScript, Vite): a typed API client, an SSE
                     parser, and the sign-in, documents and ask screens
eval/                corpus, golden dataset, gate thresholds (versioned evaluation inputs)
scripts/             smoke test, reference reports, configuration comparison
tests/               unit/ and integration/ (offline), plus live tests behind a flag
```

Dependencies point one way, layer by layer. A module may import from its own
package or a lower layer, never sideways or up, and
[test_architecture.py](../tests/unit/test_architecture.py) fails CI on any import
that breaks this:

```mermaid
flowchart TB
    subgraph L8["8 Entry point"]
        direction LR
        cli
    end
    subgraph L7["7 Applications"]
        direction LR
        api ~~~ evaluation
    end
    subgraph L6["6 Wiring"]
        direction LR
        container
    end
    subgraph L5["5 Orchestration"]
        direction LR
        rag
    end
    subgraph L4["4 Answers"]
        direction LR
        generation
    end
    subgraph L3["3 Core services"]
        direction LR
        ingestion ~~~ retrieval
    end
    subgraph L2["2 Storage, models, extraction, metrics"]
        direction LR
        db ~~~ embeddings ~~~ parsing ~~~ metrics
    end
    subgraph L1["1 Foundation"]
        direction LR
        config ~~~ text ~~~ observability
    end
    L8 --> L7 --> L6 --> L5 --> L4 --> L3 --> L2 --> L1
```

`ingestion` and `retrieval` never import each other: they share only the
database schema and the embedder. HTTP handlers hold no SQL; document queries
live in `db/documents.py`, and every processing-state change goes through
`ingestion/queue.py`.

## 4. Ingestion

Uploads are accepted at once and processed in the background (FR-3,
[ADR-0006](decisions/0006-background-ingestion.md)). The queue is the
`documents` table itself, so it needs no broker, and it survives restarts
because the upload is stored with the document until processing succeeds.

```mermaid
sequenceDiagram
    participant C as Client
    participant A as API
    participant I as IngestionService
    participant W as Worker thread
    participant P as Parser process
    participant E as Embedder
    participant D as SQLite
    C->>A: POST /v1/documents (multipart)
    A->>I: submit(filename, bytes)
    I->>I: bare filename, type, size, signature (PDF, DOCX), text decodes, SHA-256
    I->>D: look up content hash
    alt duplicate content
        I-->>A: existing document (requeued if it had failed)
        A-->>C: 200 if ready, else 202, created=false
    else new content
        I->>D: document (queued) + upload
        I->>W: wake
        A-->>C: 202, Location, status=queued
    end
    W->>D: claim: queued to processing, token, lease (compare-and-set)
    W->>P: the file on stdin (no secrets in its environment)
    P-->>W: sections as JSON, or an error (killed after RAG_PARSE_TIMEOUT_S)
    W->>W: chunk within sections, with offsets
    loop every 64 chunks
        W->>D: renew lease (or stop: back to queued)
        W->>E: embed heading path + chunk text
    end
    W->>D: one transaction, if the token still matches: ready + chunks, drop upload, delete a replaced version
    Note over D: triggers update chunks_fts and index_state.generation
    C->>A: GET /v1/documents/{id}
    A-->>C: status ready (or failed, with error)
```

```mermaid
stateDiagram-v2
    direction LR
    [*] --> queued: upload
    queued --> processing: a worker claims it
    processing --> ready: chunks written
    processing --> failed: error, or interrupted too often
    processing --> queued: shutdown hands it back
    failed --> queued: retry, or same file again
    ready --> [*]
    note right of processing
        A crashed worker's lease lapses
        and another worker takes over.
    end note
```

A document fails with an error code and message, or with `interrupted` after
`RAG_INGEST_MAX_ATTEMPTS` interruptions.

1. **Validate** (at upload). The filename is reduced to a bare name. The type
   must be `.txt`, `.md`/`.markdown`, `.pdf`, `.docx` or `.html`/`.htm`, and
   the size within `RAG_MAX_UPLOAD_BYTES`. Checks that need no parsing run here
   too: a `.pdf` must start like a PDF, a `.docx` like a ZIP file (not a legacy
   or password-protected Word file), and text and HTML must be non-blank and
   decodable. These fail the request; nothing is stored.
2. **Deduplicate** by SHA-256 of the raw bytes (FR-4). A unique index also
   covers concurrent uploads of the same file. A duplicate returns the existing
   document in whatever state it is; a failed one is queued again.
3. **Queue.** The document row (`queued`) and its upload (`document_files`)
   are written in one transaction, and the request returns.
4. **Claim.** A worker moves the oldest queued document to `processing` with
   one conditional `UPDATE` (status and token must still match), so threads and
   processes sharing the database never process the same document twice. The
   claim carries a lease of `RAG_INGEST_LEASE_S`, renewed before each embedding
   batch. If the worker dies, the lease lapses and another worker takes the
   document over; a worker that comes back after losing its claim discards its
   work. A document interrupted `RAG_INGEST_MAX_ATTEMPTS` times is failed
   (`interrupted`) instead of retried forever.
5. **Extract** ([extract.py](../src/production_rag/parsing/extract.py)) full
   text plus *sections*, in a separate parser process
   ([ADR-0007](decisions/0007-isolated-document-parsing.md)). The file goes in
   on stdin and the result comes back as JSON. The process has no secrets in
   its environment, is killed after `RAG_PARSE_TIMEOUT_S`, and on Linux and
   macOS runs under a memory limit. A file that hangs or crashes the parser
   fails its own document (`parse_timeout`, `parse_failed`) and leaves the
   service running. The sections are:
   - Markdown: one section per heading, carrying the heading path
     (`Guide > Install > Windows`). Headings inside code fences are ignored, and
     headings without a body of their own are skipped.
   - PDF: one section per page (pypdf), carrying the page number. Encrypted
     PDFs, PDFs over `RAG_MAX_PDF_PAGES`, and PDFs without a text layer (scanned)
     are rejected with a clear error.
   - HTML and DOCX ([ADR-0009](decisions/0009-docx-and-html-extraction.md)): one section per heading, carrying the
     heading path, as for Markdown. HTML headings are `h1` to `h6`; the head,
     scripts, styles and hidden elements are dropped. DOCX headings come from
     heading styles and outline levels; lists and table rows keep their shape,
     and macro-enabled files, DTDs and oversized parts are refused.
   - Text: UTF-8 (BOM tolerated), newlines normalized. Other encodings are
     rejected rather than guessed.
6. **Chunk** ([chunking.py](../src/production_rag/ingestion/chunking.py)).
   Chunks never cross a section boundary. Each is at most `RAG_CHUNK_SIZE`
   characters (default 1000) and ends at the best break in the second half of
   the window: a paragraph break, else a sentence end, else whitespace, else a
   hard cut. Consecutive chunks overlap by up to `RAG_CHUNK_OVERLAP` characters
   (default 150), and a chunk starts on a word boundary where possible.
   `text[start:end] == chunk.text` is a tested invariant, and so is
   determinism: the same text and settings give the same chunks. Each chunk
   gets a stable `chunk_key`, a hash of the document's content hash and its
   span. Re-ingesting identical bytes reproduces every key, even though row ids
   change ([test_chunk_keys.py](../tests/integration/test_chunk_keys.py)).
7. **Embed** the chunk text, prefixed with its heading path for context, in
   batches of 64.
8. **Complete** in one transaction: the document becomes `ready` with its
   extracted text (which the chunk offsets point into), its chunks are inserted,
   the stored upload is deleted, and the document it replaces, if the upload
   named one with `?replaces=`, is deleted. A failure marks the document
   `failed` with a code and a message and writes no chunks (NFR-5). Its upload
   is kept so it can be retried, and a document it replaces stays.

Raw uploads are stored in the database only until processing succeeds: while a
document is queued, processing or failed. Deleting a document deletes its
upload. Workers are threads in each API process (`RAG_INGEST_WORKERS`); the
CLI and the evaluation runner process in the calling thread (`ingest`).

## 5. Storage

SQLite through SQLAlchemy 2, with the schema owned by Alembic migrations
([versions/](../src/production_rag/db/migrations/versions/): 0001 initial schema, 0002 ingestion status, 0003 stable chunk keys).
[ADR-0002](decisions/0002-v0-storage-and-scope.md) (accepted) explains why v0
departs from ADR-0001's PostgreSQL design.

| Table | Purpose |
| --- | --- |
| `documents` | One row per unique upload: name, title, type, content hash, size, chunk count, extracted text, metadata, and processing state (status, error code and message, attempts, claim token, lease expiry, last status change) |
| `document_files` | The uploaded bytes of documents not yet ready |
| `chunks` | Text, character offsets, stable chunk key, heading path, page, embedding (float32 little-endian blob) and the embedding model id |
| `chunks_fts` | External-content FTS5 index over `chunks.text`, using the Porter stemmer |
| `index_state` | Single row with a `generation` counter |

Triggers on `chunks` keep `chunks_fts` in sync and increment
`index_state.generation` on every insert, update and delete. Foreign keys
cascade document deletes to chunks and stored uploads, and the triggers then
clean the FTS index.
Connections enable `foreign_keys`, WAL journaling and a 5 s busy timeout. An
integration test compares the ORM models with the migrated schema, so a model
change without a migration fails CI.

## 6. Retrieval

```mermaid
sequenceDiagram
    participant C as Client
    participant R as RagService
    participant V as Vector index
    participant L as FTS5
    participant X as Reranker (optional)
    participant G as Generator
    C->>R: POST /v1/query {question}
    par vector search
        R->>V: cosine, top candidate_pool
    and keyword search
        R->>L: BM25, top candidate_pool
    end
    R->>R: RRF fusion, hydrate chunks
    opt reranker configured
        R->>X: score rerank_candidates (question, passage) pairs
        X-->>R: relevance scores, then re-sort
    end
    R->>R: keep top_k
    alt nothing retrieved, or best cosine or rerank score below its threshold
        R-->>C: insufficient_evidence (no model call)
    else evidence found
        R->>G: question + one search_result block per chunk
        G-->>R: text blocks with search_result_location citations
        R-->>C: answer segments, citations, retrieved chunks, timings
    end
```

[retriever.py](../src/production_rag/retrieval/retriever.py) supports three
modes:

- **lexical:** BM25 from FTS5. The query is reduced to content terms, each term
  is quoted (which neutralizes FTS5 operators in user input), and the terms are
  OR-ed together.
- **vector:** exact cosine similarity against an in-memory matrix of the
  current embedding model's vectors. The matrix is reloaded only when
  `index_state.generation` changes, which keeps separate worker processes
  consistent without shared memory. Measured cost: about 0.5 ms per query at
  10k chunks and 6 ms at 100k.
- **hybrid** (default): both retrievers fetch `RAG_CANDIDATE_POOL` candidates
  (default 30), then Reciprocal Rank Fusion (`k = 60`) merges them. RRF uses
  only ranks, so BM25 and cosine scores never need calibrating against each
  other.

**Reranking** (optional, `RAG_RERANKER=fastembed`,
[reranking.py](../src/production_rag/retrieval/reranking.py)): the top
`RAG_RERANK_CANDIDATES` fused candidates (default 5) are scored by a local
cross-encoder (`Xenova/ms-marco-MiniLM-L-6-v2`), which reads the question and
the passage together. They are re-sorted by that score and cut to `top_k`, so
the reranker can promote a chunk from below the cut. It costs about 30 ms per
query on a laptop CPU at 5 candidates, about 250 ms at 20.
[ADR-0005](decisions/0005-reranking-and-abstention.md) has the measurements.

Embeddings: `hashing` is a deterministic, dependency-free feature-hashing
model (lexical only). `fastembed` runs `BAAI/bge-small-en-v1.5` locally through
ONNX Runtime. Each chunk records its embedding model id. Vectors from other
models are never compared, `/ready` reports them as stale chunks, and
`rag reindex` re-embeds them.
[ADR-0004](decisions/0004-embeddings-hybrid-retrieval-and-abstention.md) has
the measurements.

## 7. Generation and citations

[rag.py](../src/production_rag/rag.py) orchestrates a query:

1. Retrieve `top_k` chunks.
2. **Abstain before generation** when nothing was retrieved, when the best
   cosine is below the embedding provider's calibrated threshold, or, with a
   reranker, when the best rerank score is below the reranker's threshold. The
   result is `insufficient_evidence` with no model call. These thresholds catch
   off-topic questions. On-topic questions the corpus can't answer retrieve
   relevant-looking passages, so declining them is left to the generator
   ([evaluation.md](evaluation.md#abstention-signals)).
3. Generate.

[anthropic_generator.py](../src/production_rag/generation/anthropic_generator.py)
sends each retrieved chunk to Claude as a `search_result` content block with
citations enabled, followed by the question
([ADR-0003](decisions/0003-generation-provider-claude.md)). The system
prompt tells the model to answer only from those results, to treat their
contents as data rather than instructions, and to say plainly when they don't
answer the question. The response arrives as text blocks whose
`search_result_location` citations name the block they rely on by index, and
those indexes map directly to chunks. A citation can therefore only point at
evidence that was actually retrieved, and an out-of-range index is dropped.

The answer is returned as **segments**: text plus indexes into a
de-duplicated `citations` list, so a UI can render inline citation markers.

| Condition | `status` |
| --- | --- |
| At least one citation | `answered` |
| Text but no citations (the model declined, or answered without grounding) | `insufficient_evidence` |
| `stop_reason == "refusal"` | `refused` |

SDK errors are classified. Rate limits, 5xx responses and connection failures
are retryable (HTTP 503). Bad or missing credentials, invalid requests and
unknown models are not (HTTP 502). An error reported inside a stream is
classified by its type, since its HTTP status is the stream's 200: overload and
API errors are retryable. The SDK retries transient failures itself.

The **extractive** generator is an offline, deterministic baseline. It quotes
the retrieved sentences that overlap most with the question, each with its own
citation. It keeps the pipeline runnable and testable without credentials, but
it can't synthesize, and word overlap makes it answer some unanswerable
questions.

### Streaming

`POST /v1/query/stream` runs the same pipeline as `/v1/query`, answering with
Server-Sent Events ([ADR-0010](decisions/0010-streaming-answers-over-sse.md)):

```mermaid
sequenceDiagram
    participant C as Client
    participant A as POST /v1/query/stream
    participant R as RagService
    participant G as Claude generator
    participant M as Claude API
    C->>A: question
    A->>R: stream_answer()
    R->>R: retrieve, rerank, check the evidence
    A-->>C: event: retrieval
    alt no usable evidence
        A-->>C: event: done (insufficient_evidence, no model call)
    else evidence found
        R->>G: stream(question, chunks)
        G->>M: messages.stream, one search_result block per chunk
        loop while Claude writes
            M-->>G: text and citation deltas
            G-->>A: TextDelta, CitationAdded
            A-->>C: event: text, event: citation
        end
        G->>G: final answer from get_final_message()
        A-->>C: event: done (exactly the /v1/query response)
    end
    Note over C,M: A client that disconnects closes the stream to the Claude API
```

- Retrieval, validation, authentication and rate limits happen before the stream
  starts, so their errors are ordinary HTTP errors. A generation failure after it
  started is an `error` event.
- The Claude generator builds the final answer from `get_final_message()` with
  the code `/v1/query` uses, so `done` is exactly that response. With server-side
  fallbacks a mid-stream refusal keeps the streamed text; a refusal that stands
  replaces it in `done`.
- Closing the stream closes every iterator down to the SDK's, and with it the HTTP
  connection to the Claude API.

## 8. API, observability and security

FastAPI with sync handlers, so blocking database and model calls run in the
threadpool instead of on the event loop. Middleware assigns each request an id
(accepting a well-formed incoming `X-Request-ID`), places it in a context
variable that every log line includes, returns it as a response header, and
logs one `http.request` line with timing. Domain exceptions map to status codes
with one error shape. An unexpected exception becomes a 500 that keeps the
request id and hides internals.

Logs are one JSON object per line on stderr. The main events are
`ingest.completed`, `query.completed` (status, mode, counts, best vector score,
whether generation was skipped, per-stage timings), `generation.completed`
(model, stop reason, token usage, Anthropic request id), `generation.refused`
and `http.request`. Question text and document content are never logged
(NFR-4).

Security controls in v0: an optional static API key with constant-time
comparison, checked before the request body is read (`/health` and `/ready`
stay open for probes); per-client rate limits; request body and upload size
limits enforced as the body arrives; content checks and parsing in an isolated
process; filenames used only as metadata; bound SQL parameters and FTS5
operator neutralization; untrusted-data framing for retrieved text; and secrets
as `SecretStr`, never logged. [security.md](security.md) lists every control
and the remaining gaps.

## 9. Path to the v1 target

| Area | v0 (implemented) | v1 target | Phase |
| --- | --- | --- | --- |
| Storage | SQLite, FTS5 BM25, exact in-memory vectors (owner decision, ADR-0002) | PostgreSQL + pgvector + full-text search (ADR-0001 §2) | 5, with accounts |
| Tenancy | Open, a static key (single tenant), or accounts: Argon2id, cookie sessions, API tokens, `owner_id` in every query ([ADR-0012](decisions/0012-accounts-and-per-user-isolation.md)) | Sharing between users; sign-up and a second factor | 5 |
| Ingestion | In the background, on a SQLite-backed queue served by worker threads in each API process ([ADR-0006](decisions/0006-background-ingestion.md)) | A PostgreSQL job queue with workers on several machines | 5 |
| Deployment | One machine: `rag serve`, or the container image on ghcr.io | Several instances behind a load balancer, sharing PostgreSQL | 5 |
| File types | PDF, TXT, Markdown, DOCX, HTML (FR-2) | OCR for scanned PDFs (FR-12) | 8 |
| Answers | One response, or streamed over Server-Sent Events (FR-9, [ADR-0010](decisions/0010-streaming-answers-over-sse.md)) | Conversations (FR-11) | 4 |
| Interface | HTTP API and CLI | React + Vite web app (ADR-0001 §11) | 6 |

The v1 data model planned in Session 0:

```mermaid
erDiagram
    users ||--o{ documents : owns
    users ||--o{ sessions : has
    documents ||--o{ chunks : contains
    documents ||--o{ ingestion_jobs : processed_by
    users ||--o{ conversations : has
    conversations ||--o{ messages : contains
    messages ||--o{ message_citations : cites
    chunks ||--o{ message_citations : cited_in

    users {
        uuid id PK
        text email UK
        text password_hash
        timestamptz created_at
    }
    documents {
        uuid id PK
        uuid owner_id FK
        text original_filename
        text media_type
        bigint size_bytes
        text sha256
        text storage_key
        text status
        text error_code
        int page_count
        timestamptz created_at
    }
    chunks {
        uuid id PK
        uuid document_id FK
        uuid owner_id FK
        int ordinal
        text text
        int page_start
        int page_end
        text section_path
        int char_start
        int char_end
        int token_count
        tsvector tsv
        vector embedding
        text embedding_model
    }
    ingestion_jobs {
        uuid id PK
        uuid document_id FK
        text status
        int attempts
        text last_error
        timestamptz run_after
    }
```

`chunks.owner_id` is denormalized so vector and full-text queries filter by
owner without a join, and `(owner_id, sha256)` is unique on `documents`. v0
already stores the per-chunk fields this model needs (offsets, section path,
page, embedding model), so moving to it is a storage change rather than a
redesign. The v1 security boundaries (CSRF, CORS, rate limits, row-level
security as defense in depth) are listed in ADR-0001 §10 and
[SECURITY.md](../SECURITY.md#scope).

## 10. Known limits of v0

- SQLite means a single writer and a single node. Vector search is exact and in
  memory, which is comfortable to about 100k chunks per process.
- Retrieval-level abstention can't recognize on-topic questions the corpus
  doesn't answer; only the generator can decline those.
- Uploads are processed in the background by one worker thread per process by
  default, so a large PDF delays the documents queued behind it.
- No OCR: scanned PDFs without a text layer are rejected as having no
  extractable text.
- Rate limits are kept in each process's memory: several workers multiply them,
  and a restart resets them.
- A dropped answer stream can't be resumed; the client asks again.

## 11. Technology compatibility assessment

Whether each technology choice fits the selected architecture: one local-first Python
process on SQLite, serving the API and running ingestion
([ADR-0002](decisions/0002-v0-storage-and-scope.md),
[ADR-0006](decisions/0006-background-ingestion.md)). Checks marked *CI* run on every push on
Python 3.12, 3.13 and 3.14 ([test_compatibility.py](../tests/unit/test_compatibility.py)
and the suites named).

| Requirement of the architecture | Technology | Verdict | Evidence |
| --- | --- | --- | --- |
| One supported Python range, stated once and tested at both ends | Python ≥ 3.12 | Compatible | `requires-python`, the lockfile, Ruff and mypy targets and CI's oldest job agree on 3.12 (CI); 3.12, 3.13 and 3.14 run the full suite (CI) |
| Full-text search, cascading deletes and concurrent reads during writes, in one file | SQLite through Python's `sqlite3`: FTS5 with the Porter stemmer, foreign keys, WAL | Compatible | Capabilities checked directly (CI); migrations and retrieval tests (CI) |
| Schema changes in place, reversible | Alembic on SQLite ≥ 3.35 (`DROP COLUMN` in downgrades) | Compatible | SQLite version checked (CI); upgrade and downgrade tests (CI). Batch migrations are avoided because they would cascade-delete chunks |
| Several threads and processes sharing one database | SQLAlchemy 2 with a busy timeout; compare-and-set claims | Compatible within one machine | Two engines × two worker threads process each document once (CI); two uvicorn processes checked by hand. Not across machines, by design (ADR-0002) |
| Blocking work without blocking the event loop | FastAPI with sync handlers (thread pool); ingestion in worker threads | Compatible | API and background-ingestion tests (CI); smoke test against a real server (CI) |
| Untrusted parsing contained | pypdf, and the standard library for DOCX and HTML, in a separate process (ADR-0007, ADR-0009) | Compatible | Isolation tests for every format, hostile-file tests (CI); memory limits only on Linux and macOS |
| Works offline, no credentials | Hashing embedder and extractive generator as defaults | Compatible | Defaults test and the whole suite run offline (CI) |
| Local semantic models in the same process | fastembed and ONNX Runtime (optional extra) | Compatible | The extra installs and imports on every CI Python (CI); model quality gates run locally only, because they download models |
| A hosted answer model behind a replaceable interface | Anthropic SDK 2.x, `Generator` protocol | Compatible in code; **not verified live** | Request building, citation mapping and error mapping tested with a fake client, and streaming through the real SDK's stream parser with recorded events (CI). The API has never accepted a request from this code: the account has no credit |
| Operating systems | Linux (CI, container image), Windows (development) | Linux and Windows verified; **macOS not tested** | CI runs on Ubuntu; the suite, smoke test and real-process checks ran on Windows 11 |
| Packaging and deployment | A wheel, and a container image (`python:3.12-slim`, dependencies from `uv.lock`) | Compatible | The wheel installs on its own and runs the CLI (CI); the image is built and smoke-tested before each publish ([container.yml](../.github/workflows/container.yml)) |

Open points:

- Resolved: [ADR-0001](decisions/0001-technology-stack.md) said "Python 3.11+"; the owner
  amended it to 3.12+, which is what the package declares and CI tests. A test now keeps
  ADR-0001 in step with `requires-python`.
- Whether Claude accepts the exact request this code sends is unverified until the account
  has credit. The free token-counting check (`rag eval --plan --count-tokens`) is the
  first step.
