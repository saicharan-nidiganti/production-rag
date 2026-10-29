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
| Uploaded documents | The extracted text, in the SQLite database. The raw upload is stored there too (`document_files`) until processing succeeds; a failed document keeps it until it's retried successfully or deleted. Raw files are never written to the filesystem. | Retrieved chunks are sent to the Claude API on each query when `RAG_GENERATOR=anthropic` |
| Questions | Request memory only; never logged | Sent to the Claude API with the chunks |
| `RAG_API_KEY`, Claude API key | Environment or `.env` (git-ignored) | The Claude key goes only to the Claude API |
| Logs | stderr, JSON | Wherever the operator ships them. They contain ids, counts and timings, never document text or questions (NFR-4). |

Check that sending questions and document excerpts to Anthropic fits your data
policy before enabling the Claude generator.

## Controls

| Threat | Control | Where |
| --- | --- | --- |
| Unauthenticated access | Optional static API key (at least 16 characters), compared in constant time; `/health` and `/ready` stay open | [security.py](../src/production_rag/api/security.py), [config.py](../src/production_rag/config.py) |
| Malicious or disguised files | Extension allow-list, PDF signature check (content, not extension), strict UTF-8 for text, size and PDF page limits, encrypted PDFs rejected | [extract.py](../src/production_rag/ingestion/extract.py) |
| Path traversal | Client file names are reduced to a bare name and used only as metadata, never as a path | `safe_filename` in extract.py |
| SQL and FTS injection | Bound parameters everywhere; user text is reduced to quoted terms before reaching FTS5 | [lexical.py](../src/production_rag/retrieval/lexical.py) |
| Prompt injection in documents | Retrieved text is sent as search-result blocks; the system prompt treats it as data, not instructions; citations can only reference retrieved chunks | [anthropic_generator.py](../src/production_rag/generation/anthropic_generator.py) |
| Unintended spending | `rag eval --generation` with Claude needs `--allow-paid`; paid tests need `RAG_RUN_PAID_TESTS=1`; `rag eval --plan` previews calls and cost without generating | [cli.py](../src/production_rag/cli.py), [plan.py](../src/production_rag/evaluation/plan.py) |
| Fabricated citations | Citations are mapped from search-result indexes; out-of-range indexes are dropped; uncited answers are not reported as `answered` | same |
| Information leaks in errors | Unexpected errors return a generic 500 with a request id; upstream API error text is logged, not returned. An unexpected processing failure records only the error type on the document (`internal_error`); the details go to the log | [errors.py](../src/production_rag/api/errors.py), [pipeline.py](../src/production_rag/ingestion/pipeline.py) |
| Secrets in logs or the repository | `SecretStr` settings, never logged; `.env` git-ignored; gitleaks in CI. A test checks the Claude key appears in no log line, settings dump, evaluation report or cost plan | [ci.yml](../.github/workflows/ci.yml), [test_plan.py](../tests/integration/test_plan.py) |

## Supply chain

- Every dependency version is pinned in `uv.lock`, and CI fails if the lock is
  out of date (`uv lock --check`).
- CI runs `pip-audit` against the locked versions on every push and weekly, so
  newly published advisories surface without a code change.
- Third-party GitHub Actions are pinned to commit SHAs. First-party
  `actions/*` use major tags.
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
| No rate limiting | NFR-2 | Phase 5; until then, rate-limit at the reverse proxy |
| Upload size is enforced after the body is received | NFR-2 | Cap request bodies at the reverse proxy ([operations.md](operations.md#deploy-sequence)) |
| Prompt-injection cases exist (`partner-integrations.md` in the corpus, `t-par-*` in golden v2) but are only informative with an LLM generator | NFR-3, requirements §7 | Run them with Claude once the owner authorizes a paid evaluation |
| PDF parsing runs in a worker thread of the API process, outside the request but not isolated from it | NFR-2 | A file that crashes the process is failed (`interrupted`) after `RAG_INGEST_MAX_ATTEMPTS` tries instead of crashing it forever. Run parsing in a separate, resource-limited process before accepting untrusted uploads |
| Single static API key, no rotation overlap | — | Replaced by sessions in Phase 5 |

## Operator checklist

1. Set `RAG_API_KEY` to a random value:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Put a TLS-terminating reverse proxy in front, with a request-body limit and
   rate limits.
3. Run the service as an unprivileged user, with write access only to the
   database directory and the model cache.
4. Keep `.env` readable only by that user.
5. Restrict document uploads to trusted users: uploaded text can influence
   answers.
6. Delete failed documents you won't retry: each keeps its raw upload
   (`GET /v1/documents?status=failed` lists them).
