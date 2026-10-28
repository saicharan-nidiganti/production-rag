# Configuration

All settings are read from environment variables or from a `.env` file in the
working directory (environment variables win). Copy
[.env.example](../.env.example) to `.env` to start. Settings are defined and
validated in [config.py](../src/production_rag/config.py), and invalid values
stop the process at startup with a message naming the variable.

An **empty** value (`RAG_API_KEY=`) means the variable is unset.
`tests/unit/test_docs.py` fails if a setting is missing from this page.

## Storage

| Variable | Default | Description |
| --- | --- | --- |
| `RAG_DATABASE_URL` | `sqlite:///data/rag.db` | SQLAlchemy URL. Only SQLite is supported in v0 ([ADR-0002](decisions/0002-v0-storage-and-scope.md)). Parent directories are created. |

## Ingestion

| Variable | Default | Description |
| --- | --- | --- |
| `RAG_MAX_UPLOAD_BYTES` | `26214400` (25 MiB) | Largest accepted document (FR-2). Larger uploads get HTTP 413. |
| `RAG_MAX_PDF_PAGES` | `500` | PDFs with more pages are rejected. |
| `RAG_CHUNK_SIZE` | `1000` | Maximum chunk length in characters (at least 100). |
| `RAG_CHUNK_OVERLAP` | `150` | Characters shared by consecutive chunks; must be smaller than `RAG_CHUNK_SIZE`. |

Changing chunking settings affects only documents ingested afterwards.

## Embeddings

| Variable | Default | Description |
| --- | --- | --- |
| `RAG_EMBEDDING_PROVIDER` | `hashing` | `hashing` (lexical-only, deterministic, no download) or `fastembed` (semantic; recommended; needs the `local-embeddings` extra) |
| `RAG_EMBEDDING_DIMENSIONS` | `512` | Vector size of the `hashing` provider (at least 32) |
| `RAG_FASTEMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Model used by the `fastembed` provider |
| `RAG_FASTEMBED_CACHE_DIR` | unset (a temp directory) | Where fastembed keeps downloaded models. Set it to a persistent, writable path in production. |

Changing the provider, model or dimensions requires `rag reindex`
([operations.md](operations.md#changing-the-embedding-model)).

## Retrieval

| Variable | Default | Description |
| --- | --- | --- |
| `RAG_RETRIEVAL_MODE` | `hybrid` | `vector`, `lexical` or `hybrid`. Requests can override it per call. |
| `RAG_TOP_K` | `5` | Chunks returned by search and passed to generation (1–50). Requests can override it. |
| `RAG_CANDIDATE_POOL` | `30` | Candidates each retriever contributes before fusion |
| `RAG_RRF_K` | `60` | Reciprocal Rank Fusion constant |
| `RAG_MIN_VECTOR_SCORE` | unset | Abstain without calling the generator when the best cosine is below this value. Unset uses the calibrated default: 0.05 for `hashing`, 0.55 for `fastembed` ([evaluation.md](evaluation.md#calibrating-abstention)). |

## Generation

| Variable | Default | Description |
| --- | --- | --- |
| `RAG_GENERATOR` | `extractive` | `anthropic` (Claude, recommended) or `extractive` (offline baseline for tests and keyless development) |
| `ANTHROPIC_API_KEY` or `RAG_ANTHROPIC_API_KEY` | unset | Claude API key. Unset lets the SDK resolve credentials itself, for example from an `ant auth login` profile. |
| `RAG_ANTHROPIC_MODEL` | `claude-opus-5-5` | Claude model ID |
| `RAG_ANTHROPIC_EFFORT` | `medium` | `low`, `medium`, `high`, `xhigh` or `max`. Higher effort costs more tokens and time. |
| `RAG_ANTHROPIC_MAX_TOKENS` | `16000` | Output limit per answer, including the model's thinking |
| `RAG_ANTHROPIC_TIMEOUT_S` | `120` | Request timeout in seconds |
| `RAG_ANTHROPIC_MAX_RETRIES` | `2` | SDK retries for rate limits, overload and connection errors |
| `RAG_ANTHROPIC_FALLBACKS` | `true` | Server-side refusal fallback (beta). Set to `false` on platforms that don't support it. |

## API and logging

| Variable | Default | Description |
| --- | --- | --- |
| `RAG_API_KEY` | unset | When set, `/v1/*` requires `Authorization: Bearer <key>` or `X-API-Key: <key>`. At least 16 characters. `/health` and `/ready` stay open. |
| `RAG_LOG_LEVEL` | `INFO` | Root log level for the JSON logs on stderr |

## Variables read by other components

| Variable | Read by | Purpose |
| --- | --- | --- |
| `HF_HOME` | Hugging Face Hub (fastembed downloads) | Download cache. Set it when `~/.cache/huggingface` isn't writable. |
| `ANTHROPIC_LOG` | Anthropic SDK | `info` or `debug` for SDK request logging |
| `RAG_RUN_LIVE_TESTS` | the test suite | `1` runs tests that call the Claude API or download the embedding model |
