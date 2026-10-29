# Production RAG — Ask My Docs

A production-oriented Retrieval-Augmented Generation system designed around reliable retrieval, grounded generation, citations, evaluation, regression testing, observability, and reproducibility.

Upload your documents, ask questions in plain language, and get answers grounded in those documents. Every answer cites the exact passages it came from, and the system says so when the documents don't contain the answer.

> **Status:** the **v0 backend** is implemented: ingestion, hybrid retrieval with optional reranking, grounded generation with citations, an HTTP API, a CLI and an evaluation suite (94 questions with a held-out test split) gated in CI. Accounts, background ingestion, streaming and the web app are next. SQLite is the v0 store, and Anthropic is the configurable hosted answer provider, with an offline mode. Claude's live answer quality hasn't been measured yet. See [docs/status.md](docs/status.md) and the [roadmap](docs/roadmap.md).

---

## Overview

Production RAG is an engineering-focused RAG system built to explore what is required to move beyond a basic "retrieve and generate" prototype.

The project treats evaluation, observability, security, reproducibility, and regression detection as first-class parts of the system.

### Core goals

- Reliable document ingestion
- High-quality retrieval
- Hybrid retrieval and reranking
- Grounded answer generation
- Source citations
- Citation correctness evaluation
- Retrieval and generation evaluation
- Regression detection
- Performance benchmarking
- Structured observability
- Secure configuration and deployment
- Reproducible releases

### What v0 does

- **Ingests** `.txt`, `.md` and `.pdf` documents, idempotently by content hash. Chunks respect Markdown sections and PDF pages and keep exact character offsets.
- **Retrieves** with BM25 (SQLite FTS5), dense vectors, or both fused with Reciprocal Rank Fusion, optionally reranked by a local cross-encoder.
- **Answers** with Claude using native search-result citations. Every cited span maps back to a retrieved chunk. An answer that cites nothing is reported as `insufficient_evidence`, not `answered`.
- **Abstains** before calling the model when retrieval finds no usable evidence, using calibrated cosine and rerank-score thresholds.
- **Measures** itself: `rag eval` rebuilds an index from a versioned corpus, scores a golden dataset, writes a reproducible JSON report and fails CI on regressions.

---

## Architecture

The RAG lifecycle:

```text
Documents
    │
    ▼
Ingestion
    │
    ▼
Text Extraction
    │
    ▼
Chunking + Metadata
    │
    ▼
Embeddings / Indexing
    │
    ▼
┌─────────────────────┐
│ Retrieval           │
│                     │
│ • Vector Search     │
│ • Hybrid Retrieval  │
│ • Reranking         │
└──────────┬──────────┘
           │
           ▼
    Context Assembly
           │
           ▼
    Grounded Generation
           │
           ▼
     Citations
           │
           ▼
        Response
           │
           ├──────────────► Observability
           │
           └──────────────► Evaluation
                              │
                              ▼
                       Regression Gates
```

Components, request flows and the path to the v1 target are described in [docs/architecture.md](docs/architecture.md). Reranking is not implemented yet.

### Stack

| Layer | v0 (implemented) | v1 target ([ADR-0001](docs/decisions/0001-technology-stack.md)) |
| --- | --- | --- |
| API | Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic, uv | Same, plus a worker process |
| Storage and search | SQLite: FTS5 (BM25) + exact vector search, fused with RRF ([ADR-0002](docs/decisions/0002-v0-storage-and-scope.md)) | PostgreSQL + pgvector + full-text search, with accounts (Phase 5) |
| Reranking | Optional local cross-encoder, `ms-marco-MiniLM-L-6-v2` ([ADR-0005](docs/decisions/0005-reranking-and-abstention.md)) | Same |
| Parsing | pypdf, Markdown and plain text | Also python-docx and HTML; Tesseract OCR later |
| Embeddings | `BAAI/bge-small-en-v1.5` via fastembed (recommended), or a hashing baseline | Same |
| Generation | Claude `claude-opus-5-5` with search-result citations, or an offline extractive baseline ([ADR-0003](docs/decisions/0003-generation-provider-claude.md)) | Same, plus streaming |
| Frontend | None yet | React, TypeScript, Vite, Tailwind CSS |
| Quality | Ruff, mypy (strict), pytest, GitHub Actions, evaluation gate | Also ESLint, Playwright, gitleaks |

---

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras          # dependencies from uv.lock, including local semantic embeddings
cp .env.example .env          # then set ANTHROPIC_API_KEY, or RAG_GENERATOR=extractive

uv run rag db upgrade         # create the database schema (data/rag.db)
uv run rag ingest eval/corpus # ingest the sample handbook
uv run rag ask "How long does a canary run before the rollout continues?"
uv run rag serve              # HTTP API on http://127.0.0.1:8000 (interactive docs at /docs)
```

Without a `.env`, everything runs offline with lexical-only `hashing` embeddings and the `extractive` generator. That setup is for development, and it's what CI tests. For real use, set `RAG_EMBEDDING_PROVIDER=fastembed`, `RAG_RERANKER=fastembed` and `RAG_GENERATOR=anthropic`, as `.env.example` does.

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness (no dependencies checked) |
| `GET` | `/ready` | Readiness: database reachable and schema migrated; 503 otherwise |
| `POST` | `/v1/documents` | Upload a file (multipart `file`). 201 when created, 200 for duplicate content |
| `GET` | `/v1/documents` | List documents (`limit`, `offset`) |
| `GET`, `DELETE` | `/v1/documents/{id}` | Fetch or delete a document and its chunks |
| `POST` | `/v1/search` | `{"query", "top_k"?, "mode"?}` returns ranked chunks with scores |
| `POST` | `/v1/query` | `{"question", "top_k"?, "mode"?}` returns an answer, segments, citations, retrieved chunks and timings |

```bash
curl -s localhost:8000/v1/query -H 'Content-Type: application/json' \
  -d '{"question": "What should a client do after a 429 response?"}'
```

Response shape (illustrative and abridged):

```json
{
  "status": "answered",
  "answer": "Wait for the number of seconds in the Retry-After header, then retry with exponential backoff and jitter.",
  "segments": [{"text": "Wait for the number of seconds in the Retry-After header, ...", "citations": [0]}],
  "citations": [{"source_name": "api-rate-limits.md", "heading": "Public API Usage Limits > Handling 429 responses", "cited_text": "...", "chunk_id": 7}],
  "retrieved": ["..."],
  "model": "claude-opus-5-5",
  "timings_ms": {"retrieval": 6.1, "generation": 2140.5, "total": 2147.0}
}
```

Full request and response fields, error codes and real examples are in [docs/api.md](docs/api.md). `status` is `answered`, `insufficient_evidence` or `refused`. When `RAG_API_KEY` is set, `/v1/*` requires `Authorization: Bearer <key>` or `X-API-Key: <key>`. Errors share one shape: `{"error": {"code", "message", "request_id"}}`.

## Evaluation

```bash
RAG_EMBEDDING_PROVIDER=hashing uv run rag eval --thresholds eval/thresholds.json   # what CI runs
RAG_EMBEDDING_PROVIDER=fastembed uv run rag eval --thresholds eval/thresholds.fastembed.json
RAG_GENERATOR=extractive uv run rag eval --generation   # also score answers (free baseline)
uv run rag eval --generation --allow-paid   # with Claude: paid, one API call per question
```

Results on `eval/golden_v2.jsonl`: 94 questions over 9 documents, 18 of them unanswerable from the corpus. Thresholds are calibrated on the 36-question `dev` split; the 58-question `test` split is held out. Top-k = 5.

| Embeddings | Mode | Test hit rate | Test MRR | Unanswerable abstained (test) | False abstention | Retrieval p95 |
| --- | --- | --- | --- | --- | --- | --- |
| hashing | lexical | 0.978 | 0.909 | 0.167 | 0.000 | 1 ms |
| hashing | hybrid | 0.957 | 0.859 | 0.000 | 0.000 | 1 ms |
| fastembed (bge-small) | vector | 1.000 | 0.906 | 0.333 | 0.000 | 7 ms |
| fastembed (bge-small) | hybrid | 1.000 | 0.931 | 0.333 | 0.000 | 7 ms |
| **fastembed (bge-small)** | **hybrid + rerank** | **1.000** | **0.946** | 0.333 | 0.000 | 266 ms |

Reranking gives the best ranking, and it abstains on every off-topic test question with a wide margin. No retrieval signal recognizes on-topic questions the corpus can't answer (0 of 8 on test); declining those is the generator's job, and Claude hasn't been measured yet. `uv run python scripts/compare_configs.py` reproduces these numbers. The dataset is small and was written alongside the corpus, so treat them as a regression baseline, not a benchmark. [docs/evaluation.md](docs/evaluation.md) defines every metric and covers abstention calibration and the limitations.

## Configuration

Settings come from environment variables prefixed `RAG_`, or from a `.env` file. The most important:

| Variable | Default | Notes |
| --- | --- | --- |
| `RAG_DATABASE_URL` | `sqlite:///data/rag.db` | SQLite only in v0 |
| `RAG_EMBEDDING_PROVIDER` | `hashing` | `fastembed` recommended |
| `RAG_RETRIEVAL_MODE` | `hybrid` | `vector`, `lexical` or `hybrid` |
| `RAG_TOP_K` | `5` | Chunks passed to generation |
| `RAG_MIN_VECTOR_SCORE` | per provider | Abstention threshold; see docs/evaluation.md |
| `RAG_RERANKER` | `none` | `fastembed` for cross-encoder reranking (recommended) |
| `RAG_GENERATOR` | `extractive` | `anthropic` for Claude |
| `RAG_ANTHROPIC_MODEL` | `claude-opus-5-5` | |
| `RAG_ANTHROPIC_EFFORT` | `medium` | `low` to `max` |
| `RAG_ANTHROPIC_FALLBACKS` | `true` | Server-side refusal fallback (beta) |
| `RAG_API_KEY` | unset | Enables API-key auth |
| `RAG_MAX_UPLOAD_BYTES` | `26214400` (25 MiB) | FR-2 |

Every setting is listed in [docs/configuration.md](docs/configuration.md). Deployment, migrations and backups are covered in [docs/operations.md](docs/operations.md).

## Documentation

| Document | Contents |
| --- | --- |
| [Requirements](docs/requirements.md) | Scope, functional and non-functional requirements, constraints, success criteria |
| [Architecture](docs/architecture.md) | v0 components and flows, code layout, path to the v1 target |
| [API](docs/api.md) | Endpoints, request and response fields, errors, examples |
| [Configuration](docs/configuration.md) | Every environment variable, with defaults |
| [Decisions](docs/decisions/README.md) | Architecture Decision Records |
| [Evaluation](docs/evaluation.md) | Dataset, metrics, current results, regression gate |
| [Methodology](docs/methodology.md) | Evaluation-first engineering principles |
| [Operations](docs/operations.md) | Deployment, migrations, monitoring, backups, troubleshooting |
| [Security](docs/security.md) | Controls, data flows, supply chain, known gaps |
| [Roadmap](docs/roadmap.md) | Phase-by-phase checklist |
| [Status](docs/status.md) | Current progress, checks run, open decisions |

## Repository layout

```text
src/production_rag/   Python package: API, CLI, ingestion, retrieval, generation, evaluation, db + migrations
tests/                unit/ and integration/ tests (offline); live and paid tests behind opt-in flags
eval/                 evaluation corpus, golden dataset and regression thresholds
docs/                 requirements, architecture, decisions, evaluation, operations, roadmap, status
scripts/              reproducible experiments (configuration comparison tables)
data/                 local-only working data and the default SQLite database (contents git-ignored)
.github/              CI workflow, issue and pull request templates
```

A `frontend/` directory arrives with Phase 6.

## Development

```bash
uv sync --all-extras
uv run python -m pytest --cov                    # unit + integration tests (offline)
uv run ruff check . && uv run ruff format --check . && uv run mypy
RAG_RUN_LIVE_TESTS=1 uv run python -m pytest tests/integration/test_live.py   # model download (free)
RAG_RUN_PAID_TESTS=1 uv run python -m pytest tests/integration/test_live.py   # Claude API (paid)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch and commit conventions, code standards and testing expectations.

## Security

Please report vulnerabilities privately. See [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
