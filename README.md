# Production RAG

A production-oriented Retrieval-Augmented Generation backend designed around reliable retrieval,
grounded generation, citations, evaluation, regression testing, observability and
reproducibility.

> **Status:** the first backend milestone is implemented: ingestion, hybrid retrieval, grounded
> generation with citations, an HTTP API, a CLI and an evaluation suite with a CI regression
> gate. See [docs/roadmap.md](docs/roadmap.md) for what's next.

---

## What it does

- **Ingests** `.txt`, `.md` and `.pdf` documents. Ingestion is idempotent by content hash. Chunks
  respect Markdown sections and PDF pages and keep exact character offsets.
- **Retrieves** with BM25 (SQLite FTS5), dense vectors, or both fused with Reciprocal Rank
  Fusion.
- **Answers** with Claude using native search-result citations. Every cited span maps back to a
  retrieved chunk, and an answer that cites nothing is reported as `insufficient_evidence`, not
  `answered`.
- **Abstains** before calling the model when retrieval finds no usable evidence.
- **Measures** itself: `rag eval` rebuilds an index from a versioned corpus, scores a golden
  dataset, writes a reproducible JSON report and fails CI on regressions.

```text
Documents ─► Extract (sections, pages) ─► Chunk (offsets) ─► Embed ─► SQLite (+FTS5)
                                                                          │
Question ─► Vector search ─┐                                              │
          ─► BM25 search  ─┴► RRF fusion ─► Abstain? ─► Claude + search_result blocks
                                                              │
                                     Answer segments + citations ─► JSON logs, timings
```

Read [docs/architecture.md](docs/architecture.md) for the design and
[docs/decisions/](docs/decisions/) for the reasoning behind each choice.

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras          # dependencies, including local semantic embeddings
cp .env.example .env          # then set ANTHROPIC_API_KEY (or RAG_GENERATOR=extractive)

uv run rag db upgrade         # create the database schema
uv run rag ingest eval/corpus # ingest the sample handbook
uv run rag ask "How long does a canary run before the rollout continues?"
uv run rag serve              # HTTP API on http://127.0.0.1:8000 (docs at /docs)
```

Without `.env`, everything runs offline: lexical-only `hashing` embeddings and the `extractive`
generator. That setup is useful for development and is what CI tests. For real use, set
`RAG_EMBEDDING_PROVIDER=fastembed` and `RAG_GENERATOR=anthropic`.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness (no dependencies checked) |
| `GET` | `/ready` | Readiness: database reachable and schema migrated; 503 otherwise |
| `POST` | `/v1/documents` | Upload a file (multipart `file`). 201 when created, 200 for duplicate content |
| `GET` | `/v1/documents` | List documents (`limit`, `offset`) |
| `GET`/`DELETE` | `/v1/documents/{id}` | Fetch or delete a document and its chunks |
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
  "citations": [{"source_name": "api-rate-limits.md", "heading": "Public API Usage Limits > Handling 429 responses", "cited_text": "...", "chunk_id": 7, "...": "..."}],
  "retrieved": ["..."],
  "model": "claude-opus-5-5",
  "timings_ms": {"retrieval": 6.1, "generation": 2140.5, "total": 2147.0}
}
```

`status` is `answered`, `insufficient_evidence` or `refused`. When `RAG_API_KEY` is set, `/v1/*`
requires `Authorization: Bearer <key>` or `X-API-Key: <key>`. Errors share one shape:
`{"error": {"code", "message", "request_id"}}`.

## Evaluation

```bash
uv run rag eval --thresholds eval/thresholds.json --output eval/reports   # what CI runs
RAG_EMBEDDING_PROVIDER=fastembed uv run rag eval --thresholds eval/thresholds.fastembed.json
uv run rag eval --generation   # also score generated answers (uses the configured generator)
```

Results on `eval/golden_v1.jsonl`: 36 questions over 6 documents, including 6 that the corpus
can't answer. Top-k = 5.

| Embeddings | Mode | Hit rate | Recall | MRR | Retrieval p95 |
|---|---|---|---|---|---|
| hashing | lexical | 0.967 | 0.967 | 0.900 | 1.2 ms |
| hashing | vector | 0.900 | 0.883 | 0.817 | 1.1 ms |
| hashing | hybrid | 0.967 | 0.950 | 0.906 | 1.3 ms |
| fastembed (bge-small) | vector | 1.000 | 1.000 | 0.934 | 6.3 ms |
| **fastembed (bge-small)** | **hybrid** | **1.000** | **1.000** | **0.957** | 7.5 ms |

The dataset is small and was written alongside the corpus, so treat these numbers as a
regression baseline, not a benchmark. [docs/evaluation.md](docs/evaluation.md) defines every
metric, covers abstention calibration and lists the limitations.

## Configuration

Settings come from environment variables prefixed `RAG_` (or a `.env` file). The most important:

| Variable | Default | Notes |
|---|---|---|
| `RAG_DATABASE_URL` | `sqlite:///data/rag.db` | SQLite only in this release ([ADR 0002](docs/decisions/0002-storage.md)) |
| `RAG_EMBEDDING_PROVIDER` | `hashing` | `fastembed` recommended ([ADR 0004](docs/decisions/0004-embeddings-and-evaluation.md)) |
| `RAG_RETRIEVAL_MODE` | `hybrid` | `vector`, `lexical` or `hybrid` |
| `RAG_TOP_K` | `5` | Chunks passed to generation |
| `RAG_MIN_VECTOR_SCORE` | per provider | Abstention threshold; see docs/evaluation.md |
| `RAG_GENERATOR` | `extractive` | `anthropic` for Claude |
| `RAG_ANTHROPIC_MODEL` | `claude-opus-5-5` | |
| `RAG_ANTHROPIC_EFFORT` | `medium` | `low` to `max` |
| `RAG_ANTHROPIC_FALLBACKS` | `true` | Server-side refusal fallback (beta) |
| `RAG_API_KEY` | unset | Enables API-key auth |
| `RAG_MAX_UPLOAD_BYTES` | `10485760` | |

All settings, with descriptions, are in [config.py](src/production_rag/config.py). Operational
guidance is in [docs/operations.md](docs/operations.md).

## Development

```bash
uv sync --all-extras
uv run python -m pytest --cov        # unit + integration tests (offline, ~7 s)
uv run ruff check . && uv run ruff format --check . && uv run mypy
RAG_RUN_LIVE_TESTS=1 uv run python -m pytest tests/integration/test_live.py   # real API + model download
```

```text
src/production_rag/
  api/          FastAPI app, routes, schemas, auth, error mapping
  db/           SQLAlchemy models, engine, Alembic migrations
  ingestion/    extraction, chunking, ingestion pipeline
  retrieval/    vector index, FTS5 search, RRF fusion, retriever
  generation/   Claude generator, extractive baseline, answer types
  evaluation/   dataset, metrics, runner, regression gate
  rag.py        query orchestration and abstention
  cli.py        `rag` command
eval/           corpus, golden dataset, gate thresholds
docs/           architecture, decisions, evaluation, operations, roadmap
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for conventions.

## License

MIT. See [LICENSE](LICENSE).
