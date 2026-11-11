# Security

How v0 protects data, what it doesn't protect yet, and how the supply chain is
checked. To report a vulnerability, follow [SECURITY.md](../SECURITY.md); don't
open a public issue.

## Scope of v0

v0 is **single tenant**. Everyone who holds the API key can read, upload and
delete every document. Don't expose it to mutually untrusted users. Accounts
and per-user isolation (FR-1, NFR-1) arrive in Phase 5
([ADR-0002](decisions/0002-v0-storage-and-scope.md)).

## Assets and data flows

| Asset | Where it lives | Leaves the host? |
| --- | --- | --- |
| Uploaded documents | The extracted text, in the SQLite database. The raw upload is stored there too (`document_files`) until processing succeeds; a failed document keeps it until it's retried successfully or deleted. The application never writes raw files to the filesystem, but while an upload is being received, Starlette spools anything over 1 MB to a temporary file in the OS temp directory, deleted when the request ends (a crash can leave one behind). | Retrieved chunks are sent to the Claude API on each query when `RAG_GENERATOR=anthropic` |
| Questions | Request memory only; never logged | Sent to the Claude API with the chunks |
| `RAG_API_KEY`, Claude API key | Environment or `.env` (git-ignored) | The Claude key goes only to the Claude API |
| Logs | stderr, JSON | Wherever the operator ships them. They contain ids, counts and timings, never document text or questions (NFR-4). Security events (`security.*`) also record the client address. |

Check that sending questions and document excerpts to Anthropic fits your data
policy before enabling the Claude generator.

## Controls

| Threat | Control | Where |
| --- | --- | --- |
| Unauthenticated access | Optional static API key (at least 16 characters), compared in constant time. It is checked before the request body is read, and every path needs it except `/health`, `/ready` and the API docs. Failures are logged as `security.auth_failed` (missing or invalid, with the client address, never the key) | [guard.py](../src/production_rag/api/guard.py), [security.py](../src/production_rag/api/security.py), [config.py](../src/production_rag/config.py) |
| Excessive use and key guessing | Per-client rate limits (token buckets) for uploads and retries, questions, searches and other `/v1` requests, checked after the key and before the body is read; over one, 429 with `Retry-After`. Failed API-key attempts have their own limit: a client out of attempts gets 429 without its key being compared. Logged as `security.rate_limited` (operation and client address). The limits are per process ([operations.md](operations.md#rate-limits)) | [ratelimit.py](../src/production_rag/api/ratelimit.py), [guard.py](../src/production_rag/api/guard.py) |
| The web interface | Served on the API's own origin, so no CORS is configured. Its files are public and hold no data; everything shown comes from `/v1` behind the key. The page is sent with `Content-Security-Policy: default-src 'self'` (no inline or third-party scripts, no framing), `nosniff` and `no-referrer`, and makes no third-party requests. The key is kept in `sessionStorage` for the tab only, sent as a bearer header, never in a URL. Answers and passages are rendered as text, never as HTML ([ADR-0011](decisions/0011-web-interface-served-by-the-backend.md)) | [web.py](../src/production_rag/api/web.py), [client.ts](../web/src/api/client.ts) |
| Information exposed through monitoring | `GET /metrics` needs the API key like `/v1`, and draws from the `read` rate limit. Metric labels take values from fixed sets (route templates, status codes, models, stages), never questions, file names, document ids or client addresses; logs carry no content ([observability.md](observability.md#sensitive-data)) | [metrics.py](../src/production_rag/metrics.py), [monitoring.py](../src/production_rag/api/monitoring.py) |
| Oversized requests | Bodies are limited as they arrive: a declared `Content-Length` over the limit gets 413 before anything is read, and a body without one is counted and cut off at the limit. Uploads may be `RAG_MAX_UPLOAD_BYTES` plus 64 KiB of framing, other bodies `RAG_MAX_REQUEST_BYTES` (64 KiB). Logged as `security.request_too_large` | [guard.py](../src/production_rag/api/guard.py) |
| Malicious or disguised files | Extension allow-list, PDF and DOCX signature checks (content, not extension), strict UTF-8 for text, a declared encoding at most for HTML, size and PDF page limits, encrypted PDFs rejected. DOCX: legacy, password-protected and macro-enabled files refused; a part with a DTD or entity declaration refused before parsing (no entity expansion); uncompressed part size and entry count limits against ZIP bombs; the document streamed, never loaded whole. HTML: scripts never run, and scripts, styles and hidden elements are dropped ([ADR-0009](decisions/0009-docx-and-html-extraction.md)) | [extract.py](../src/production_rag/parsing/extract.py), [docx_text.py](../src/production_rag/parsing/docx_text.py), [html_text.py](../src/production_rag/parsing/html_text.py) |
| Parser exploits and resource exhaustion | Parsing runs in a separate process: killed after `RAG_PARSE_TIMEOUT_S`, address-space and CPU limits on Linux and macOS, no secrets in its environment, results returned as JSON (never unpickled). A bad file fails only its own document | [isolation.py](../src/production_rag/parsing/isolation.py), [ADR-0007](decisions/0007-isolated-document-parsing.md) |
| Path traversal | Client file names are reduced to a bare name and used only as metadata, never as a path | `safe_filename` in extract.py |
| SQL and FTS injection | Bound parameters everywhere; user text is reduced to quoted terms before reaching FTS5 | [lexical.py](../src/production_rag/retrieval/lexical.py) |
| Prompt injection in documents | Retrieved text is sent as search-result blocks; the system prompt treats it as data, not instructions; citations can only reference retrieved chunks | [anthropic_generator.py](../src/production_rag/generation/anthropic_generator.py) |
| Unintended spending | `rag eval --generation` with Claude needs `--allow-paid`; paid tests need `RAG_RUN_PAID_TESTS=1`; `rag eval --plan` previews calls and cost without generating | [cli.py](../src/production_rag/cli.py), [plan.py](../src/production_rag/evaluation/plan.py) |
| Fabricated citations | Citations are mapped from search-result indexes; out-of-range indexes are dropped; uncited answers are not reported as `answered` | same |
| Information leaks in errors | Unexpected errors return a generic 500 with a request id; upstream API error text is logged, not returned. An unexpected processing failure records only the error type on the document (`internal_error`); the details go to the log. Validation errors don't echo the rejected input. Database errors never quote their bound values (SQLAlchemy `hide_parameters`), so document text can't reach the log through one | [errors.py](../src/production_rag/api/errors.py), [pipeline.py](../src/production_rag/ingestion/pipeline.py), [engine.py](../src/production_rag/db/engine.py) |
| Secrets in logs or the repository | `SecretStr` settings, never logged; `.env` git-ignored; gitleaks in CI. As a backstop, the log formatter replaces the configured secrets with `[REDACTED]` wherever they appear, exception text included. A test checks the Claude key appears in no log line, settings dump, evaluation report or cost plan | [observability.py](../src/production_rag/observability.py), [ci.yml](../.github/workflows/ci.yml), [test_plan.py](../tests/integration/test_plan.py) |

## Audit, 2026-11-10

A review of the code, API, configuration, dependencies, image, storage, logging, CI and the
RAG-specific surfaces before the next release. Each area lists what was checked and the
evidence; the findings follow.

| Area | Checked | Result |
| --- | --- | --- |
| Dependencies | `pip-audit` against every locked version, on each push and weekly | No known vulnerabilities at `a153e33` |
| Dependencies | Every runtime dependency is used; `prometheus-client`, added this cycle, has no dependencies of its own and is Apache-2.0 and BSD-2-Clause | No unused or unsupported package found |
| Secrets | gitleaks on every pushed range in CI, and over the full history on 2026-10-29; `.env` is git-ignored and `.env.example` holds no values | No real credential has been committed. Two fingerprinted ignores cover made-up test values |
| Secrets | Logs redact configured secrets; error bodies never include exception text; the image is built from an allow-list (`.dockerignore`) | Tested: `tests/unit/test_config_and_logging.py`, `tests/integration/test_api.py` |
| API | The key, rate limits and body size are checked before a body is read; every route but the probes needs the key (a test walks the OpenAPI schema); validation errors don't echo input | Tested: `tests/integration/test_request_guard.py`, `test_api.py` |
| API | No CORS headers are sent, so browsers on other origins can't read responses; no debug or reload mode exists | Confirmed in `api/app.py` and `cli.py` |
| RAG | Retrieved text reaches the model only as `search_result` blocks, apart from the instructions, with untrusted-data framing; citations can only point at retrieved chunks; file names and metadata aren't sent as instructions | Tested with a fake client; **not yet measured with Claude** (the injection cases need the paid run) |
| RAG | Retrieval can't cross tenants because v0 has one tenant | A known gap, below |
| Storage | Bound parameters everywhere and FTS5 operators neutralized; statement errors hide their parameters | Tested: `tests/unit/test_embeddings_and_fusion.py`, `tests/integration/test_database.py` |
| Storage | Backups carry a SHA-256 and are verified before a restore | Tested: `tests/integration/test_maintenance.py` |
| Parsing | Each upload is parsed in a separate process without the service's secrets, with a time limit, and a memory limit on Linux and macOS | Tested: `tests/integration/test_parsing_isolation.py` |
| Image | Runs as uid 10001, has no secrets, installs only locked dependencies | Checked by the container workflow on every build |
| CI | Workflows default to read-only permissions; third-party actions are pinned to commit SHAs; publishing uses the job's own token | Confirmed in `.github/workflows/` |

### Findings

| Finding | Severity | Resolution |
| --- | --- | --- |
| The interactive API docs and OpenAPI schema are served without the API key | Low: the schema is public in this repository, but a deployment may not want to advertise its API | Fixed: `RAG_API_DOCS_ENABLED=false` removes them (operator checklist, item 8) |
| No static analysis of the code for security problems | Medium: a class of bug nothing looked for | Fixed: CodeQL with the `security-extended` queries runs on every push and weekly |
| The published image wasn't scanned, and had no bill of materials | Medium: a vulnerable base image would ship unnoticed | Fixed: the container workflow fails on a critical or high vulnerability that has a fix, and attaches an SBOM and build provenance to the published image |
| The first scan found two high-severity `zlib1g` vulnerabilities (CVE-2026-76844, CVE-2026-85091) whose fixes the `python:3.12-slim` tag hadn't picked up. The published 0.2.0 image has them. No Python package had a finding | High in the base system | Fixed for new builds: the image applies the distribution's security updates when it is built. Republishing 0.2.0 from the same source is the owner's decision |
| The image's base wasn't kept current | Low | Fixed: Dependabot now watches the `Dockerfile` |
| The database and its backups aren't encrypted by the application | Medium, by deployment | Documented as an operator control ([data-lifecycle.md](data-lifecycle.md#privacy-and-security)); listed under known gaps |
| Prompt-injection resistance is unmeasured with a real model | Medium | Open: needs the paid evaluation, which the owner has authorized and which waits for API credit |

No critical or high finding remains open in code. The open items are the known gaps below.

## Supply chain

- Every dependency version is pinned in `uv.lock`, and CI fails if the lock is
  out of date (`uv lock --check`).
- CI runs `pip-audit` against the locked versions on every push and weekly, so
  newly published advisories surface without a code change.
- Third-party GitHub Actions are pinned to commit SHAs. First-party
  `actions/*` use major tags. Dependabot proposes updates for the actions and
  for the image's base.
- CodeQL analyzes the Python code with the `security-extended` queries on every
  push, pull request and weekly; findings appear under the repository's code
  scanning alerts.
- The container workflow scans each image with Trivy before publishing and fails
  on a critical or high vulnerability that has a fix. To accept one, add its id to
  `.trivyignore` with the reason and a date to revisit it. Published images carry
  an SPDX bill of materials and build provenance:
  `docker buildx imagetools inspect ghcr.io/saicharan-nidiganti/production-rag:TAG --format '{{ json .SBOM }}'`.
- Runtime dependency licenses are permissive (MIT, BSD, Apache-2.0, PSF) or
  MPL-2.0, which is compatible when the packages are used unmodified
  (requirements §5). The last check was on 2026-10-29.
- Models are downloaded from Hugging Face at runtime, not redistributed with
  the code. Embeddings: `BAAI/bge-small-en-v1.5`, MIT (fastembed fetches
  Qdrant's ONNX export, also MIT). Reranker: `Xenova/ms-marco-MiniLM-L-6-v2`,
  an ONNX conversion of `cross-encoder/ms-marco-MiniLM-L-6-v2`, which is
  Apache-2.0; the conversion states no license of its own. Checked on the
  model cards on 2026-10-29. Model files aren't hash-pinned: pre-download them
  into the image ([operations.md](operations.md#install-and-configure)) to fix
  the version you run.
- The evaluation corpus is synthetic, written for this project (MIT).

## Known gaps

| Gap | Requirement | Plan |
| --- | --- | --- |
| No accounts or per-user isolation | FR-1, NFR-1 | Phase 5, with PostgreSQL ([ADR-0001](decisions/0001-technology-stack.md) §10) |
| Rate limits are per process and in memory: `--workers N` allows a client N times a limit, a restart resets them, and clients behind a proxy that isn't in `--forwarded-allow-ips` share one allowance | NFR-2 | Rate-limit at the reverse proxy for a limit across processes; a shared store comes with PostgreSQL in Phase 5 |
| Uploads over 1 MB pass through a temporary file while they are received | — | Keep the temp directory private to the service user (for example systemd `PrivateTmp=true`) |
| Prompt-injection cases exist (`partner-integrations.md` in the corpus, `t-par-*` in golden v2) but are only informative with an LLM generator | NFR-3, requirements §7 | Run them with Claude once the owner authorizes a paid evaluation |
| The parser process is isolated from the service but not sandboxed: it can reach the network and the filesystem, and on Windows has no memory limit | NFR-2 | Add a sandbox (no network, read-only filesystem) before accepting anonymous uploads ([ADR-0007](decisions/0007-isolated-document-parsing.md)) |
| Single static API key, no rotation overlap | — | Replaced by sessions in Phase 5 |
| The application doesn't encrypt the database or its backups | Data at rest | Put `/data` on an encrypted volume and encrypt backups before they leave the host ([data-lifecycle.md](data-lifecycle.md#privacy-and-security)) |
| Deleted content stays in free database pages and in backups until they are rewritten or expire | Erasure | `VACUUM` with the service stopped, and backup retention ([data-lifecycle.md](data-lifecycle.md#deletion)) |

## Operator checklist

1. Set `RAG_API_KEY` to a random value:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Put a TLS-terminating reverse proxy in front, with a request-body limit and
   rate limits that hold across processes, and list it in uvicorn's
   `--forwarded-allow-ips` so the built-in limits apply per client.
3. Run the service as an unprivileged user, with write access only to the
   database directory and the model cache.
4. Keep `.env` readable only by that user.
5. Restrict document uploads to trusted users: uploaded text can influence
   answers.
6. Delete failed documents you won't retry, since each keeps its raw upload: schedule
   `rag db prune --failed-older-than 30`, or list them with
   `GET /v1/documents?status=failed`.
7. Schedule `rag db backup --keep 7`, copy the backups off the host encrypted, keep them
   readable only by the service user, and test a restore regularly
   ([data-lifecycle.md](data-lifecycle.md#backup)).
8. Set `RAG_API_DOCS_ENABLED=false` unless you want the API's schema and its interactive
   docs readable without the key.
9. Put `/data` on an encrypted volume.
