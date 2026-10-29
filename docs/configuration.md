# Configuration

All settings are read from environment variables or from a `.env` file in the
working directory (environment variables win). Copy
[.env.example](../.env.example) to `.env` to start. Settings are defined and
validated in [config.py](../src/production_rag/config.py), and invalid values
stop the process at startup with a message naming the variable.

An **empty** value (`RAG_API_KEY=`) means the variable is unset.
`tests/unit/test_docs.py` fails if a setting is missing from this page.

## Environments

There are no environment profiles: the application has no `RAG_ENV` switch and doesn't know
which environment it runs in. Every environment is the same code with different settings,
supplied from outside, from highest to lowest priority: environment variables, then a
`.env` file in the working directory, then the defaults below. That order is tested
(`test_environment_variables_override_dotenv`).

What each environment uses, and how much of it is enforced:

| Environment | How it is configured | Enforced by code or tests | Recommended, not enforced |
| --- | --- | --- | --- |
| Development | The defaults, or a `.env` copied from `.env.example` | Defaults run fully offline: hashing embeddings, extractive generator, no reranker, no credentials (`test_defaults_are_a_safe_offline_development_setup`) | `RAG_EMBEDDING_PROVIDER=fastembed` and `RAG_RERANKER=fastembed` once the models are downloaded |
| Test | `tests/conftest.py` removes every `RAG_*` variable, runs each test in an empty directory (no `.env`) on a temporary database, and parses in-process for speed | Paid tests need `RAG_RUN_PAID_TESTS=1`, model downloads `RAG_RUN_LIVE_TESTS=1` | — |
| Evaluation | `rag eval` builds a fresh index in a temporary database from `eval/corpus/`; the dataset, corpus fingerprint, configuration and commit are written into every report | It never touches `RAG_DATABASE_URL` (`test_evaluation_never_touches_the_configured_database`); Claude runs need `--allow-paid` | Pin providers on the command line (`make eval-gate` does), so a `.env` can't change the configuration being gated |
| Production | Environment variables from the process manager or a secret store | Invalid settings stop startup; a non-loopback `rag serve` without `RAG_API_KEY` prints a warning | The settings in [operations.md](operations.md#install-and-configure), as in `.env.example`: fastembed, the reranker, Claude, `RAG_API_KEY`, a database on a persistent volume |

`.env.example` is the reference production-like configuration: a test loads it as valid
settings and checks that every key, token and secret in it is empty
(`test_env_example_is_valid_and_holds_no_secrets`).

## Validation and secrets

Settings are validated once, at startup, before anything is built. A bad value stops the
process with one line per problem, naming the variable and never repeating its value, because
the value may be a secret:

```text
invalid configuration (environment variables or .env):
  - RAG_TOP_K: Input should be greater than or equal to 1
  - RAG_API_KEY: RAG_API_KEY must be at least 16 characters; generate one with: ...
```

`rag` exits with code 2; the API refuses to start. Rules that involve several settings are
checked too: `RAG_CHUNK_OVERLAP` below `RAG_CHUNK_SIZE`, and `RAG_PARSE_TIMEOUT_S` below
`RAG_INGEST_LEASE_S`.

No setting is required: every one has a working default. The Claude credentials are the one
conditional requirement, and they can't be checked at startup, because the SDK can also find
them in an `ant auth login` profile. With `RAG_GENERATOR=anthropic` and no credentials, the
first question fails with HTTP 502 "no Claude API credentials configured".

Secrets (`ANTHROPIC_API_KEY`, `RAG_API_KEY`) are held as secret values. They are never
logged, never shown in settings dumps or evaluation reports (tested), and never passed to the
parser process. `.env` and key files are git-ignored (`test_secret_files_are_ignored_by_git`).

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
| `RAG_INGEST_WORKERS` | `1` | Background ingestion threads in each API process (0–16). `0` processes nothing in that process: uploads stay `queued` until a process with workers picks them up. More threads rarely help, because embedding already uses every CPU core. |
| `RAG_INGEST_POLL_INTERVAL_S` | `2` | How often idle workers check for queued documents, in seconds. A process wakes its own workers on upload, so this mainly sets how quickly *other* processes pick up work and how long a crashed worker's documents wait after their lease lapses. |
| `RAG_INGEST_LEASE_S` | `300` | A worker's claim on a document lapses this many seconds after its last renewal; another worker then takes the document over. Workers renew before each batch of 64 chunks, so this only needs to exceed the slowest single step (extracting a large PDF). |
| `RAG_PARSE_ISOLATION` | `process` | `process` parses each upload in a separate Python process without secrets in its environment, killed after `RAG_PARSE_TIMEOUT_S` ([ADR-0007](decisions/0007-isolated-document-parsing.md)). `inline` parses in the worker thread; the test suite uses it for speed. |
| `RAG_PARSE_TIMEOUT_S` | `120` | Seconds before the parser process is killed and the document fails with `parse_timeout`. Must be shorter than `RAG_INGEST_LEASE_S`, which is checked at startup. |
| `RAG_PARSE_MEMORY_MB` | `1024` | Address-space limit of the parser process on Linux and macOS (minimum 128); a file that needs more fails with `parse_failed`. Windows has no such limit: only the timeout applies. |
| `RAG_INGEST_MAX_ATTEMPTS` | `3` | Times a document may be interrupted (its worker stopped mid-way) before it is marked `failed` with code `interrupted`. Ordinary errors fail at once and are never retried automatically. |

Changing chunking settings affects only documents ingested afterwards. Processing states,
recovery and retries are described in [operations.md](operations.md#background-ingestion).

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
| `RAG_RERANKER` | `none` | `fastembed` reranks retrieved candidates with a local cross-encoder (recommended; needs the `local-embeddings` extra and downloads ~80 MB on first use). Adds about 30 ms per query on a laptop CPU with the default 5 candidates ([ADR-0005](decisions/0005-reranking-and-abstention.md)). |
| `RAG_RERANKER_MODEL` | `Xenova/ms-marco-MiniLM-L-6-v2` | Cross-encoder used by the `fastembed` reranker |
| `RAG_RERANK_CANDIDATES` | `5` | Fused candidates the reranker scores before keeping `RAG_TOP_K`. 20 recovers one dev paraphrase question that 5 misses, with the same held-out results, at about 7× the rerank latency (p50 232 ms against 33 ms in the reference run; [trade-offs](evaluation.md#reranker-trade-offs)). |
| `RAG_MIN_RERANK_SCORE` | unset | With a reranker, also abstain when the best rerank score is below this value. Unset uses the calibrated default: −8.4 for `Xenova/ms-marco-MiniLM-L-6-v2`; other models have no default and never abstain on this score. |

## Generation

| Variable | Default | Description |
| --- | --- | --- |
| `RAG_GENERATOR` | `extractive` | `anthropic` (Claude, recommended) or `extractive` (offline baseline for tests and keyless development) |
| `ANTHROPIC_API_KEY` or `RAG_ANTHROPIC_API_KEY` | unset | Claude API key. Unset lets the SDK resolve credentials itself, for example from an `ant auth login` profile. |
| `RAG_ANTHROPIC_MODEL` | `claude-opus-5-5` | Claude model ID |
| `RAG_ANTHROPIC_EFFORT` | `medium` | `low`, `medium`, `high`, `xhigh` or `max`. Higher effort costs more tokens and time. |
| `RAG_ANTHROPIC_MAX_TOKENS` | `16000` | Output limit per answer, including the model's thinking. It sets the worst-case cost of a paid evaluation (`rag eval --plan`). |
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
| `RAG_RUN_LIVE_TESTS` | the test suite | `1` runs tests that download the embedding model (free) |
| `RAG_RUN_PAID_TESTS` | the test suite | `1` runs tests that call the Claude API (paid) |
