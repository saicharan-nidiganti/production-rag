# Changelog

Notable changes, grouped by kind. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). 0.1.0 is the first release; its "Changed"
and "Fixed" entries describe changes made during development before it, kept for the
record. Detailed per-session notes and verification results are in
[docs/status.md](docs/status.md); how a release is cut is in
[docs/release.md](docs/release.md).

## [Unreleased]

### Changed

- **Keyword search on PostgreSQL is 3.4 times faster, with the same results**
  ([ADR-0015](docs/decisions/0015-search-vectors-in-the-row.md)). Each chunk's search vector
  was stored out of line, because it is larger than the text, so every match cost a lookup;
  migration 0006 keeps it in the row. At 100,045 chunks, keyword search's p50 went from 172
  to 51 ms. Retrieval p95 went from 197 to 85 ms with approximate vector search, and from
  380 to 256 ms with exact search, which now meets the 300 ms target there too. New chunks
  get the layout at once; `rag db rebuild-search-index` rewrites existing ones on
  PostgreSQL (drop the HNSW index first, or pass `--keep-vector-index`).

### Security

- With accounts on SQLite, the service warns at startup (`startup.shared_keyword_statistics`)
  that keyword ranking uses every user's documents' word statistics. The known gap now
  says that it shows in the order of a user's own results, not only in their scores, so
  hiding scores wouldn't close it; PostgreSQL's ranking doesn't have it.

## [1.1.0] - 2026-11-12

### Added

- **Approximate vector search on PostgreSQL** (`RAG_VECTOR_SEARCH=approximate`,
  [ADR-0014](docs/decisions/0014-approximate-vector-search.md)). One HNSW index per
  embedding model, built with `rag db create-vector-index` while uploads and searches carry
  on, and dropped with `rag db drop-vector-index`. A search limited to one owner keeps
  scanning until it has found that owner's chunks (pgvector's iterative scans, so 0.8 or
  later), and one that still comes up short is redone exactly and counted in
  `rag_vector_exact_fallbacks_total`. `/ready` and the startup log report the index. At
  100,000 chunks retrieval p95 fell from 380 ms to 197 ms; with bge-small the index
  returned 0.98 of exact search's top 30 and matched it on hybrid retrieval. Exact search
  stays the default, and PostgreSQL still searches exactly where that is cheaper.
- **`rag bench` measures the index**: with approximate search it builds the index, and
  reports the build's time and size, recall against exact search with the index plan
  forced, and how often the planner chose the index. CI and the release gate this at
  10,000 chunks (`eval/bench_thresholds.approximate.json`, release criterion
  `vector-index`).
- `RAG_HNSW_EF_SEARCH` sets how many candidates the index search keeps (default 200).
- **The container workflow builds and scans a commit before it is tagged**: run by hand
  without a release, it builds, tests and scans the current commit, and never publishes.
  Every build also keeps a full vulnerability report, at every severity and fixed or not,
  as its `image-vulnerabilities` artifact.

### Changed

- Benchmark and evaluation reports record `vector_search` and `hnsw_ef_search` in their
  configuration; the CI baseline records `vector_search: exact`.

### Security

- The image no longer includes the base image's pip, which the service never uses and
  which had five medium and one low advisory with fixes. A fresh build has no finding
  with a fix at any severity; the high-severity advisories without one are assessed in
  [docs/security.md](docs/security.md#review-for-110-2026-11-12).
- Documented a known gap: on SQLite, keyword scores are computed with statistics from
  every user's documents.

### Fixed

- **Retrieval on SQLite was about four times slower than it should be, since accounts
  (1.0.0).** Every API search is limited to an owner, the shared owner while accounts are
  off, and both searches took a slow path for it. Vector search copied the owner's part of
  the in-memory matrix on every search: 7.7 ms instead of 0.65 at 10,000 chunks. Keyword
  search joined every match to its document before ranking: 30 ms instead of 6. The
  benchmark's HTTP p95 at four clients went from about 50 ms to between 75 and 135. Vector
  search now slices a matrix ordered by owner. Keyword search ranks through FTS5's index
  and keeps the owner's matches in the same statement, widening the scan until it has the
  owner's best *k*, so a small library isn't crowded out by a large one.
- `rag bench` asks its in-process questions as the shared owner, as the API does. It asked
  as no owner, which skipped both slow paths and hid the regression from those numbers.
- The README's roadmap still listed the 1.0 release as upcoming.

## [1.0.0] - 2026-11-12

### Added

- **Release criteria and an automated release** (`docs/release-criteria.md`,
  `.github/workflows/release.yml`). A version tag runs every check as its own step: tests
  on both databases, the web interface, the smoke test, four quality gates, the performance
  gate, dependency audits, a full-history secret scan, code-scanning alerts, the package and
  a reproducible rebuild. `scripts/release_checklist.py` records each as pass, fail, warn or
  not measured, and the release is published only if nothing blocking failed, with the
  package, checksums, signed build provenance and every report attached. Run by hand, it
  is a dry run.
- **A gate for answer assembly and citations** (`eval/thresholds.generation.json`), with
  the extractive generator, so the citation code is checked without a paid model.
- **PostgreSQL with pgvector**, alongside SQLite: set `RAG_DATABASE_URL` to
  `postgresql+pg8000://...` and install the `postgres` extra (pg8000, BSD-3-Clause); the
  image includes it. Vector search runs in the database with pgvector, keyword search with
  a generated `tsvector` column and `ts_rank`, both with the owner filter in the same
  statement. Evaluation and the benchmark run on the configured kind of database. The
  suite and the retrieval gate run on both databases in CI
  ([ADR-0013](docs/decisions/0013-postgresql-with-pgvector.md)).
- **Accounts and per-user isolation** (FR-1, NFR-1), with `RAG_ACCOUNTS_ENABLED=true`
  ([ADR-0012](docs/decisions/0012-accounts-and-per-user-isolation.md)). Users sign in with
  an email address and password (Argon2id) and get an `HttpOnly`, `SameSite=Strict` session
  cookie with a CSRF token, or use personal API tokens from scripts. Every document has an
  owner, and listings, lookups, searches, answers and citations are limited to the caller's
  own documents, with the filter inside both searches. Administrators manage users
  (`/v1/users`, `rag users`). Off by default; with it off nothing changes. Migration 0005
  adds the tables and `documents.owner_id`; run `rag db upgrade`. Adds the `argon2-cffi`
  dependency.
- **Web interface** (FR-10, roadmap Phase 6). A single page in `web/` (React, TypeScript,
  Vite, Tailwind CSS), served by the backend at `/` when `RAG_WEB_DIR` points at its build;
  the container image includes it. A document library (upload by drag and drop or a
  picker, status, failure reasons, retry, replace, delete) and an ask page where the answer
  streams in, each claim carries a numbered source, and a source opens its passage with
  the cited text highlighted and its file, section, page and position. It asks for the API
  key only when the server needs one. Served with a same-origin Content-Security-Policy
  ([ADR-0011](docs/decisions/0011-web-interface-served-by-the-backend.md)).
- **docs/deployment.md**: the supported deployment targets, hardware requirements measured
  by the benchmark, environments, the release process and reliability requirements.
- **docs/runbooks.md**: incident severity levels, the first five minutes, runbooks for a
  service that is down, provider failures, wrong or unsupported answers, slowness, stuck
  ingestion, a damaged database and security incidents, plus rollback, verifying recovery,
  the incident record and the review afterwards.
- **Security scanning**: CodeQL analyzes the code on every push and weekly. The container
  workflow scans each image with Trivy and fails on a critical or high vulnerability that
  has a fix; published images carry an SBOM and build provenance. Dependabot watches the
  image's base. `docs/security.md` records the 2026-11-10 audit and its findings.
- **`RAG_API_DOCS_ENABLED`**: `false` removes `/docs`, `/redoc` and `/openapi.json`, which
  are served without the API key.
- **Performance benchmark**: `rag bench` builds a temporary index of the evaluation
  corpus and seeded synthetic documents (10,000 chunks by default), ingests a generated
  100-page PDF, asks the golden questions in process and streamed, and load-tests a real
  `rag serve` with concurrent clients. It reports p50, p95 and p99 per stage, throughput,
  CPU and memory where the platform exposes them, database size, and tokens and estimated
  cost when the generator reports them, as JSON and Markdown. The gate
  (`eval/bench_thresholds.json`) applies the requirements' targets as limits and compares
  with a baseline: pass, warn or fail, with exit code 3 when the run itself fails. CI runs
  it on every push (`make bench`).
- **Backups, verification and restores**: `rag db backup` makes an online copy with
  SQLite's backup API, checks it, and writes a manifest with its schema revision, counts
  and SHA-256; `--keep N` prunes older ones. `rag db verify` checks the database or a backup
  (checksum, SQLite integrity, foreign keys, schema, full-text index, chunk and upload
  invariants, vector sizes) without writing to it. `rag db restore --yes` refuses a backup
  that fails verification, keeps the database it replaces, and moves the index generation
  forward so running processes reload their vectors. `rag db rebuild-search-index`
  repairs the full-text index.
- **Retention**: `rag db prune --failed-older-than DAYS [--dry-run]` deletes failed
  documents and their stored uploads.
- **Lineage**: each document's `metadata.ingested_with` records the version and the chunk
  size and overlap that produced its chunks.
- **New versions of a document**: `POST /v1/documents?replaces={id}` deletes the old
  version in the transaction that makes the new one ready, so searches never see both or
  neither; if the new version fails, the old one stays. Documents gain a `replaces` field.
  Migration 0004 adds the column: run `rag db upgrade`, and back up first, since rolling
  back to 0.2.0 afterwards means restoring that backup.
- **docs/data-lifecycle.md**: data inventory and classification, storage, lineage,
  versioning, retention, deletion, backup, recovery procedures, privacy and growth.
- **Prometheus metrics** at `GET /metrics`: requests and latency by route
  template, answer outcomes and per-stage latency, model tokens, generation
  failures and refusals, ingestion outcomes, security refusals, and the
  queue's state (documents by status, chunks, stale chunks, the oldest queued
  document's wait, live workers, database and schema health). Behind the API
  key; `RAG_METRICS_ENABLED=false` turns it off. Adds the `prometheus-client`
  dependency. The smoke test and the container image check scrape it.
- **docs/observability.md**: every log event and metric, request correlation,
  Prometheus alert rules, diagnosing retrieval and generation problems, and
  what is never recorded. Tracing is deferred, with its spans defined.
- **Container image** on the GitHub Container Registry, `ghcr.io/saicharan-nidiganti/production-rag`, with
  tags `X.Y.Z`, `X.Y` and `latest`. Built from `uv.lock`, it runs
  `rag serve --migrate` as an unprivileged user, keeps its data in `/data`
  and has a health check. `.github/workflows/container.yml` builds it, checks
  that it becomes ready, ingests, answers with citations and streams, and only
  then publishes it, for every release tag (or by hand for an earlier one).
  Published for 0.2.0 from that release's source.

### Changed

- **Schema revision 0005** (from 0003): document replacement, the account tables and an
  owner on every document. Run `rag db upgrade` after backing up; existing documents stay in
  the shared space.
- **`rag db`** is a group of commands. `rag db upgrade` and `rag db current` work as before.
- **`rag eval` and `rag bench`** build their throwaway index on the configured kind of
  database, and record it in their reports.
- **`/metrics`** is served by default, behind the API key when one is set.
- **The container image** serves the web interface at `/` and includes the PostgreSQL
  driver.

### Security

- The container image applies the distribution's security updates when it is built. The
  first image scan found two high-severity `zlib1g` vulnerabilities (CVE-2026-76844,
  CVE-2026-85091) in the base image of 0.2.0.

### Fixed

- A manual container build of an earlier release used that release's files on top of the
  current tree, keeping files added since. It now uses exactly the release's source.

## [0.2.0] - 2026-11-10

### Added

- **Streamed answers** (FR-9). `POST /v1/query/stream` answers with Server-Sent
  Events: `retrieval` first, then `text` and `citation` as the answer is
  written, then `done` with exactly the `/v1/query` response, or `error` if
  generation fails after the stream started. `done` is authoritative: a
  refused answer replaces the streamed text. `rag ask --stream` prints the
  answer as it is written. With Claude, the final answer is built from the
  accumulated message with the same code as `/v1/query`, and a client that
  disconnects closes the stream to the Claude API. The stream counts against
  the question rate limit
  ([ADR-0010](docs/decisions/0010-streaming-answers-over-sse.md)).
- **DOCX and HTML uploads** (FR-2, #6). `.docx`, `.html` and `.htm` are
  ingested like Markdown: one section per heading, so citations carry the
  heading path. DOCX headings come from heading styles or outline levels;
  lists and table rows keep their shape; text boxes are read once; deleted
  text of tracked changes and field codes are dropped. HTML drops the head,
  scripts, styles and hidden elements, and may declare a non-UTF-8 encoding.
  Both are parsed with the standard library in the isolated parser process
  ([ADR-0009](docs/decisions/0009-docx-and-html-extraction.md)). Refused:
  legacy `.doc`, password-protected and macro-enabled Word files, DOCX parts
  with a DTD or over the size limits, and archives with too many entries.

### Changed

- With `RAG_API_KEY` set, every path except `/health`, `/ready` and the API
  docs requires the key, including unknown ones (401 instead of 404).
- `rag ingest` checks a file's size before reading it.

### Security

- **Rate limits per client** (#30, NFR-2). Each client address gets a token
  bucket per kind of request: uploads and retries (30 a minute), questions
  (60), searches (120), other `/v1` requests (600) and failed API-key attempts
  (10). Over a limit the API answers 429 `rate_limited` with `Retry-After`,
  before the body is read, and logs `security.rate_limited`. A client out of
  key attempts gets 429 without its key being compared. The limits live in
  each process's memory; [operations.md](docs/operations.md#rate-limits)
  explains what that means with several workers or behind a proxy. New
  settings `RAG_RATE_LIMIT_*_PER_MINUTE`; `0` turns a limit off.
- **Checks before the body is read** (#30). The API key is now checked before
  a request's body is received, so an unauthenticated upload is refused
  without being read or spooled to disk; before, FastAPI parsed the whole
  upload first. Request bodies are limited as they arrive: a `Content-Length`
  over the limit gets 413 at once, and a chunked body is cut off at the limit.
  New setting `RAG_MAX_REQUEST_BYTES` (64 KiB) for bodies other than uploads.
- **Security events in the logs**: `security.auth_failed` and
  `security.request_too_large`, with the client address but never the key or
  the body.
- Database errors no longer quote their bound values (document text,
  questions) in their messages, which reached the log for unexpected errors.
- The log formatter redacts the configured secrets wherever they would
  appear, as a backstop.
- Validation errors no longer echo the rejected input back in `details`.

## [0.1.0] - 2026-10-29

### Added

- **Ingestion** of `.txt`, Markdown and PDF documents. Chunks follow Markdown
  sections and PDF pages and keep exact character offsets. Duplicate uploads
  are detected by SHA-256.
- **Background ingestion** with a status per document (`queued`, `processing`,
  `ready`, `failed` with a code and message), retries, and recovery after
  crashes and restarts, on a queue in the SQLite database
  ([ADR-0006](docs/decisions/0006-background-ingestion.md)).
- **Hybrid retrieval**: BM25 (SQLite FTS5) and dense vectors fused with
  Reciprocal Rank Fusion; embeddings from `bge-small-en-v1.5` (fastembed) or an
  offline hashing baseline.
- **Optional cross-encoder reranking** (`ms-marco-MiniLM-L-6-v2`), scoring 5
  candidates by default ([ADR-0005](docs/decisions/0005-reranking-and-abstention.md)).
- **Grounded answers** from Claude with native search-result citations mapped
  to stored passages, and an offline extractive generator
  ([ADR-0003](docs/decisions/0003-generation-provider-claude.md)).
- **Abstention** before generation, on calibrated cosine and rerank-score
  thresholds.
- **Isolated parsing**: each upload is parsed in a separate process with a
  timeout, no secrets in its environment and, on Linux and macOS, a memory
  limit; a bad file fails only its own document
  ([ADR-0007](docs/decisions/0007-isolated-document-parsing.md)).
- **HTTP API** (FastAPI) with optional API-key authentication, request ids and
  a common error envelope; the `rag` CLI.
- **Evaluation**: golden v2 (94 questions with a held-out test split, including
  unanswerable, prompt-injection and conflicting-evidence cases), retrieval and
  answer metrics, reproducible reports and regression gates in CI.
- **Spending guard** for paid runs: `--allow-paid`, the `paid` test marker, and
  `rag eval --plan` to preview calls and cost without generating anything.
- **Quality system**: Ruff, mypy `--strict`, pytest with a 90% coverage floor,
  an import-layering test, a real-server smoke test, a package check, secret
  scanning, dependency auditing and Markdown lint in CI; a `Makefile` shared by
  developers and CI.
- Documentation: requirements, architecture, API, configuration, operations,
  security, evaluation, ADRs 0001–0008, roadmap and status.
- **Stable chunk keys** (`chunk_key`): the same for identical content and chunk
  settings across re-ingestion, unlike row ids (migration 0003).
- `rag eval` prints a readable summary (`--json` for the previous output), and records a
  failed generation per case instead of aborting the run.
- Reference evaluation reports for every compared configuration
  (`eval/results/2026-10-29/`, written by `scripts/reference_results.py`).
- ADR-0008 (accepted): the architecture-style evaluation, confirming the local-first
  modular monolith. ADR-0001's Python floor corrected from 3.11+ to 3.12+.

### Changed

- Reranking scores 5 candidates by default instead of 20: the same held-out
  results at about a seventh of the latency (owner decision).
- Uploads return `202 Accepted` and are processed in the background, instead of
  `201` after synchronous processing.
- The ingestion queue's claim protocol (`ingestion/queue.py`) and the document
  queries (`db/documents.py`) are separate modules; route handlers hold no SQL.
- Text extraction moved from `production_rag.ingestion.extract` to
  `production_rag.parsing.extract`; `production_rag.ingestion` still re-exports
  its public names.
- One-shot CLI commands log warnings only unless `RAG_LOG_LEVEL` is set.

### Fixed

- Invalid settings printed the rejected value in a traceback, including a too-short API
  key; errors now name the variable only, and `rag` exits with code 2.
- The deployment smoke test crashed with a traceback on malformed responses instead of
  failing cleanly.
- A made-up API key in a test tripped the secret scan in CI.
- Two architecture diagrams didn't render on GitHub (a `;` inside a Mermaid
  message); a docs test now catches it.
- `rag ask` printed citation markers that swallowed the following space.
- Every CI job failed after the `Makefile` was introduced, because a recipe
  contained a literal newline; regenerated and checked with GNU make.
- Document timestamps read back from SQLite lacked a time zone; they are now
  always UTC.
- An empty `RAG_API_KEY=` enabled authentication with an empty key, and
  `ANTHROPIC_API_KEY` in `.env` never reached the SDK.
- PDF uploads were validated by extension only; they are now checked by content.
- Possessives produced a stray single-letter token that matched unrelated text.
- Validation errors didn't use the common error envelope.
- The CLI treated a directory named like a document (`x.md`) as a file.

### Security

- Raw uploads are stored in the database until processing succeeds (and kept
  for failed documents until retried or deleted); documented in
  [docs/security.md](docs/security.md).
- Third-party GitHub Actions are pinned to commit SHAs and kept current by
  Dependabot.

[Unreleased]: https://github.com/saicharan-nidiganti/production-rag/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/saicharan-nidiganti/production-rag/releases/tag/v1.1.0
[1.0.0]: https://github.com/saicharan-nidiganti/production-rag/releases/tag/v1.0.0
[0.2.0]: https://github.com/saicharan-nidiganti/production-rag/releases/tag/v0.2.0
[0.1.0]: https://github.com/saicharan-nidiganti/production-rag/releases/tag/v0.1.0
