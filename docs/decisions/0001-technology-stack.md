# ADR-0001: Technology stack for v1

- **Status:** Proposed, 2026-10-28. Merged to `main` on 2026-10-29 together with
  the v0 backend. Merging is not acceptance: the project owner accepts this ADR
  explicitly. The v0 implementation departs from §2 (storage), §9 (background
  ingestion) and §10 (authentication) as proposed in
  [ADR-0002](0002-v0-storage-and-scope.md). The generation model provider (§8)
  is pending decision D-2; [ADR-0003](0003-generation-provider-claude.md)
  proposes one.
- **Deciders:** Project owner, with engineering analysis
- **Supersedes:** none

## Context

Production RAG ("Ask My Docs") needs document ingestion, hybrid retrieval,
grounded generation with verifiable citations, multi-user isolation, a web
interface and an evaluation harness. See [requirements.md](../requirements.md).

Constraints that shape the choices:

- One developer, minimal budget, local development on Windows and CI on Linux
- A public, MIT-licensed repository, so dependencies must be permissively licensed
- An evaluation-first methodology: add a component only when it measurably helps
- Isolation between users is a hard requirement, not a feature

## Decision drivers

1. Correctness and evidence traceability
2. Simplicity: the fewest moving parts that still meet the requirements
3. Security: isolation enforced where the data lives
4. Cost: runs for free locally, with paid services optional
5. Measurability: every retrieval stage can be swapped and evaluated

## Options and decisions

### 1. Backend language and framework

| Option | Notes |
| --- | --- |
| **Python + FastAPI** ✅ | Most of the RAG/ML ecosystem is in Python (parsers, embeddings, evaluation). Pydantic validation and generated OpenAPI. Async streaming support. |
| Python + Django | Batteries included, but heavier, and its ORM fits pgvector and custom SQL less well. |
| Node.js (Express/Nest) | Good for APIs, but parsing, embedding and evaluation tooling would be split across two languages. |

**Decision:** Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic. Dependencies managed with uv.

### 2. Storage and vector index

| Option | Notes |
| --- | --- |
| **PostgreSQL + pgvector** ✅ | One transactional store for users, documents, chunks, vectors and full-text search. Ownership filters are plain SQL `WHERE` clauses in the same query as the similarity search. HNSW indexes. Free, and available as a managed service almost everywhere. |
| Qdrant / Weaviate / Milvus | Strong dedicated vector engines with payload filtering, but a second datastore to keep consistent with Postgres. |
| Chroma / FAISS | Good for prototypes. Weaker on multi-user filtering, durability and operations. |
| Pinecone (managed) | Paid, and document vectors leave our infrastructure. |
| Elasticsearch / OpenSearch | Excellent BM25 plus vectors, but heavy to run for a single developer. |

**Decision:** PostgreSQL 16+ with pgvector.

**Trade-off:** Postgres full-text ranking (`ts_rank_cd`) is not true BM25.
This is acceptable for v1. Keyword quality is measured in Phase 3 and revisited
in Phase 7 if it limits retrieval.

### 3. Hybrid retrieval fusion

**Decision:** Reciprocal Rank Fusion (RRF, initial `k = 60`) over vector and
full-text candidate lists. It needs no score calibration between the two
retrievers. Candidate depth, `k` and the final evidence count are configuration
values, tuned by evaluation.

### 4. Document parsing

| Option | Notes |
| --- | --- |
| **pypdf / pdfplumber** ✅ | Permissive licenses (BSD/MIT). Per-page text, plus layout details from pdfplumber when needed. |
| PyMuPDF | Fast and accurate, but **AGPL**, which conflicts with the MIT license. Excluded. |
| unstructured / Docling | Richer layout and table handling, but heavier (ML models, many dependencies). Considered later if extraction quality is the bottleneck. |

**Decision:** pypdf and pdfplumber for PDF, python-docx for DOCX, an HTML
parser with sanitization for HTML, and plain readers for TXT and Markdown.
Tesseract OCR for scanned PDFs in Phase 8.

### 5. Chunking

**Decision:** structure-aware chunking. Split on headings, then paragraphs, then
sentences, within a token budget (starting point: about 500 tokens with about
15% overlap). Each chunk keeps its page range, section path, character offsets
and a content hash. Sizes are tuned by evaluation, not fixed by assumption.

### 6. Embeddings

| Option | Notes |
| --- | --- |
| **Local sentence-transformers model (default: `BAAI/bge-small-en-v1.5`, 384-dim)** ✅ | Free. Runs on CPU. Documents never leave the machine. Deterministic in CI. |
| Hosted embedding APIs (OpenAI, Voyage, Cohere) | Potentially higher quality, but recurring cost, a network dependency, and document text is sent to a third party. |

**Decision:** an `EmbeddingProvider` interface, with the local model as the
default. Hosted providers can be added behind the same interface and adopted
only if evaluation shows a worthwhile gain. Each stored vector records its model
name and dimension, so changing models is an explicit, migratable re-embedding
job.

### 7. Reranking

**Decision:** none in the baseline. In Phase 7, evaluate a local cross-encoder
reranker (and optionally a hosted rerank API) against the baseline. Adopt it only
if the quality gain justifies the added latency.

### 8. Generation model

**Decision:** an `LLMProvider` interface with streaming support. The default
provider is **pending decision D-2** (a paid API or a local model via Ollama).
Requirements for any provider: streaming, a context window of at least 32k
tokens, and reliable instruction following for the citation format. Automated
tests use a deterministic in-process test provider and never call paid APIs.

### 9. Background ingestion

| Option | Notes |
| --- | --- |
| **PostgreSQL-backed job table + worker process** ✅ | Uses `SELECT … FOR UPDATE SKIP LOCKED`. Durable, retryable, and needs no extra infrastructure. |
| Celery / RQ / Arq + Redis | Mature, but adds Redis and another service to operate. |
| FastAPI `BackgroundTasks` only | Simple, but jobs are lost on restart and there are no retries. |

**Decision:** a Postgres-backed queue with a separate worker process. Revisit if
throughput requires it.

### 10. Authentication and isolation

**Decision:** first-party email-and-password accounts. Passwords are hashed with
Argon2id. Server-side sessions use HttpOnly, Secure, SameSite cookies, with CSRF
protection on state-changing requests. Every data-access function takes the
owner's ID and filters by it in SQL. PostgreSQL row-level security is evaluated
in Phase 5 as an extra layer of defense. External identity providers are out of
scope for v1.

### 11. Frontend

| Option | Notes |
| --- | --- |
| **React + TypeScript + Vite + Tailwind CSS** ✅ | A simple single-page app for an authenticated tool. Fast builds. Types generated from the API's OpenAPI schema. |
| Next.js | Server-side rendering and routing are not needed behind a login, and it adds a second server runtime. |
| Server-rendered HTML / HTMX | Lightest option, but a poorer fit for streaming chat and an interactive citation viewer. |

**Decision:** React, TypeScript, Vite and Tailwind CSS. Vitest for unit tests and
Playwright for end-to-end tests.

### 12. Quality, security and operations tooling

**Decision:** Ruff, mypy (strict), pytest, ESLint and `tsc`; GitHub Actions CI;
gitleaks for secret scanning; pip-audit and npm audit for dependencies;
structured JSON logging with request IDs and per-stage timings; Docker Compose
for local services. OpenTelemetry is considered in Phase 8.

### 13. Evaluation

**Decision:** an in-repo evaluation harness, using a versioned golden dataset
(JSONL) and computing retrieval metrics (Recall@k, MRR, nDCG), citation
validity and precision, groundedness, and abstention accuracy. LLM-as-judge
metrics use a pinned judge model and prompt, and are reported separately from
deterministic metrics. Third-party evaluation frameworks may be used as a
reference, but the core metrics are not coupled to them.

## Consequences

### Positive

- A single datastore keeps documents, chunks and vectors consistent, and makes
  ownership filtering a property of each query.
- The whole stack runs locally for free. Paid services are optional.
- Each retrieval stage sits behind an interface and can be measured and replaced.

### Negative and risks

- Postgres full-text ranking is weaker than BM25. This is mitigated by
  measuring it and revisiting in Phase 7.
- A local embedding model may trail hosted models on quality. This is mitigated
  by the provider interface plus evaluation.
- Running Postgres locally on Windows needs Docker Desktop or a native install
  (decision D-3).
- Running embedding models locally adds CPU load and model-download time to
  ingestion.

## Revisit when

- More than about 5 million chunks, or p95 retrieval latency targets are missed
- Evaluation shows keyword retrieval or embeddings are the limiting factor
- Sharing between users or organizations enters scope
