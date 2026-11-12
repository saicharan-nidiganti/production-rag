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
| `RAG_DATABASE_URL` | `sqlite:///data/rag.db` | `sqlite:///path` (parent directories are created), or `postgresql+pg8000://user:password@host:5432/database`, which needs the `postgres` extra and pgvector 0.6 or later on the server ([ADR-0013](decisions/0013-postgresql-with-pgvector.md)). The password is redacted from logs. |

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
| `RAG_VECTOR_SEARCH` | `exact` | PostgreSQL only. `approximate` searches the embedding model's HNSW index instead of comparing the query with every chunk. Build the index first with `rag db create-vector-index`; until it exists, searches stay exact. Needs pgvector 0.8 or later, for iterative index scans. Worth it past about 30,000 chunks per owner, with a real embedding model ([operations.md](operations.md#approximate-vector-search), [ADR-0014](decisions/0014-approximate-vector-search.md)). |
| `RAG_HNSW_EF_SEARCH` | `200` | With approximate search, the candidates the index search keeps (pgvector's `hnsw.ef_search`; 1–1000, and at least `RAG_CANDIDATE_POOL`). Higher is slower and closer to exact: with bge-small, 100 returned 0.96 of exact search's top 30, 200 returned 0.98 and 400 returned 0.99. |

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
| `RAG_API_KEY` | unset | When set, every route except `/health`, `/ready` and the API docs (`/docs`, `/redoc`, `/openapi.json`) requires `Authorization: Bearer <key>` or `X-API-Key: <key>`, checked before the request body is read. At least 16 characters. |
| `RAG_ACCOUNTS_ENABLED` | `false` | Require a signed-in user for every `/v1` route, and keep each user's documents, searches and answers apart. Users sign in with an email address and password, or send a personal API token. Can't be combined with `RAG_API_KEY`. Create the first administrator with `rag users create --admin`. |
| `RAG_SESSION_TTL_HOURS` | `12` | How long a browser sign-in lasts (1 to 720). |
| `RAG_SESSION_COOKIE_SECURE` | `true` | Send the session cookie over HTTPS only. Set `false` only to test over plain HTTP. |
| `RAG_PASSWORD_MIN_LENGTH` | `12` | Shortest password accepted (8 to 128). |
| `RAG_MAX_REQUEST_BYTES` | `65536` (64 KiB) | Largest request body outside uploads (search and question JSON; minimum 1024). Uploads are limited by `RAG_MAX_UPLOAD_BYTES` plus 64 KiB for multipart framing. Both are enforced while the body arrives: a larger declared `Content-Length` gets 413 before anything is read. |
| `RAG_RATE_LIMIT_UPLOAD_PER_MINUTE` | `30` | Uploads and retries (`POST /v1/documents`, `POST /v1/documents/{id}/retry`) per client per minute. |
| `RAG_RATE_LIMIT_QUERY_PER_MINUTE` | `60` | Questions (`POST /v1/query` and `/v1/query/stream`, which can call a paid model) per client per minute. |
| `RAG_RATE_LIMIT_SEARCH_PER_MINUTE` | `120` | Searches (`POST /v1/search`) per client per minute. |
| `RAG_RATE_LIMIT_READ_PER_MINUTE` | `600` | Every other `/v1` request per client per minute. |
| `RAG_RATE_LIMIT_AUTH_FAILURES_PER_MINUTE` | `10` | Missing or wrong API keys per client per minute, when `RAG_API_KEY` is set. Past it, the client gets 429 without its key being checked, even a correct one, until an attempt refills. |
| `RAG_WEB_DIR` | unset | The directory holding the built web interface (`web/dist`). When set, the interface is served at `/`; its files need no API key, and everything it shows comes from `/v1`. The container image sets it to `/app/web`. |
| `RAG_API_DOCS_ENABLED` | `true` | Serve the interactive API docs (`/docs`, `/redoc`) and the OpenAPI schema (`/openapi.json`), which need no API key. `false` removes all three; they answer 404. |
| `RAG_METRICS_ENABLED` | `true` | Serve Prometheus metrics at `GET /metrics`, behind the API key when one is set. `false` makes the route answer 404. The metrics are in [observability.md](observability.md). |
| `RAG_LOG_LEVEL` | `INFO` | Root log level for the JSON logs on stderr. The configured secrets are replaced by `[REDACTED]` wherever they would appear in a log line. |

Rate limits are token buckets per client address: a client may use a minute's allowance at
once, and it refills continuously. Over a limit the API answers 429 `rate_limited` with
`Retry-After`, before the body is read. `0` turns a limit off. The buckets live in each API
process's memory, so with `--workers N` a client can make up to N times a limit and a restart
resets them ([operations.md](operations.md#rate-limits)).

## Variables read by other components

| Variable | Read by | Purpose |
| --- | --- | --- |
| `HF_HOME` | Hugging Face Hub (fastembed downloads) | Download cache. Set it when `~/.cache/huggingface` isn't writable. |
| `ANTHROPIC_LOG` | Anthropic SDK | `info` or `debug` for SDK request logging |
| `RAG_RUN_LIVE_TESTS` | the test suite | `1` runs tests that download the embedding model (free) |
| `RAG_RUN_PAID_TESTS` | the test suite | `1` runs tests that call the Claude API (paid) |
