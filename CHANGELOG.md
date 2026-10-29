# Changelog

Notable changes, grouped by kind. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions will follow
[Semantic Versioning](https://semver.org/) once releases start. Nothing has been
released yet: `pyproject.toml` says `0.1.0`, and everything below is unreleased.
Detailed per-session notes and verification results are in
[docs/status.md](docs/status.md).

## [Unreleased]

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
  security, evaluation, ADRs 0001–0006, roadmap and status.

### Changed

- Reranking scores 5 candidates by default instead of 20: the same held-out
  results at about a seventh of the latency (owner decision).
- Uploads return `202 Accepted` and are processed in the background, instead of
  `201` after synchronous processing.
- The ingestion queue's claim protocol (`ingestion/queue.py`) and the document
  queries (`db/documents.py`) are separate modules; route handlers hold no SQL.

### Fixed

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
