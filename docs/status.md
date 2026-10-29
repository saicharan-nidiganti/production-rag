# Project Status

Last updated: 2026-10-29

## Current state

**v0 backend: implemented and on `main`.** Background ingestion (PDF, TXT,
Markdown) with a status per document, hybrid retrieval (BM25 + vectors with
RRF) with optional cross-encoder reranking, grounded generation with Claude
search-result citations, an HTTP API, the `rag` CLI, and an evaluation suite of
94 questions with a held-out test split, gated in CI. See [architecture.md](architecture.md) and the
[roadmap](roadmap.md).

**Next milestone: measured grounded answers.** Run the first Claude evaluation
on golden v2. That measures answered rate, citation precision, prompt-injection
resistance and, most importantly, whether Claude declines the on-topic
unanswerable questions that no retrieval signal catches. Those behaviors of
[ADR-0003](decisions/0003-generation-provider-claude.md) count as verified only
once that run passes its acceptance gate (`eval/thresholds.claude.json`). It is
prepared but blocked twice: the Anthropic account has no API credit, and the
run needs the owner's explicit authorization (D-4). The
[plan](evaluation.md#paid-evaluation-with-claude) gives the exact commands, call
counts and costs.

## Requirements snapshot

Measured on golden v2's held-out test split, recommended configuration (fastembed + reranker):

| Area | State |
| --- | --- |
| Retrieval targets (Recall@5 ≥ 0.85, MRR ≥ 0.70) | Met: hit rate 1.000, MRR 0.946 (CI configuration: 0.957 / 0.859) |
| Citation validity 100% | Enforced in code: citations map to retrieved chunks by index |
| False abstention ≤ 0.10 | Met: 0.00 at retrieval level, on both splits and in every configuration |
| Correct abstention on unanswerable ≥ 0.90 | **Not met yet.** Retrieval abstains on 4 of 4 off-topic test questions but 0 of 8 on-topic ones; the extractive baseline declines 4 of 12. Claude, which has to handle the on-topic ones, is unmeasured ([evaluation.md](evaluation.md#abstention-signals)) |
| Prompt-injection suite (0 successful injections) | Cases exist (`t-par-*`); only informative with Claude, so **unverified** |
| Accounts and isolation (FR-1, NFR-1) | Not started; v0 is single tenant ([ADR-0002](decisions/0002-v0-storage-and-scope.md)) |
| Ingestion status (FR-3) | Met: `queued` → `processing` → `ready` or `failed` with a code and message, retry, recovery after crashes and restarts ([ADR-0006](decisions/0006-background-ingestion.md)) |
| Streaming (FR-9), DOCX/HTML (FR-2) | Not started |

## Session log

### Session 6 — Repository audit and engineering quality (2026-10-29)

Audited the repository, code, CI, documentation and GitHub metadata against the
actual implementation, then fixed what the audit found. No paid API calls.

- **Architecture.** The ingestion queue's claim protocol moved to
  `ingestion/queue.py` (pipeline.py: 593 → about 330 lines), and document
  queries to `db/documents.py`, so route handlers hold no SQL. A new
  layering test checks every import against the documented layers; it was
  verified by injecting an upward import, which first exposed a gap in the
  test itself (`from production_rag import cli` went unseen), since fixed.
- **Quality system.** A `Makefile` shared by developers and CI; a real-server
  smoke test (`scripts/smoke_test.py`: readiness, a 202 upload, background
  ingestion, search, a cited answer), verified to fail when ingestion fails;
  a package job that installs the wheel alone and runs `rag db upgrade`,
  `ingest` and `search` from it; a test that the held-out split doesn't leak
  from the calibration split, with a positive control; coverage floor 85% →
  90%; Dependabot for the pinned GitHub Actions.
- **A broken push, caught by CI:** the first `Makefile` (commit `3ad09f2`)
  failed every quality and package job, because generating it lost its
  backslashes. Fixed in `e535329` and checked with GNU make 4.3 before
  pushing; CI then passed.
- **Documentation.** README rewritten. The old one said reranking wasn't
  implemented, listed gitleaks as future work and quoted 20-candidate
  latency. The new one has a status table, architecture and query-flow
  diagrams, real measured results and real API output. The diagrams were
  reviewed by hand; rendering them locally was blocked. Also added: a
  documentation index (`docs/README.md`) with a shared status vocabulary,
  `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1 by reference),
  `CHANGELOG.md` from the git history, and third-party model licenses,
  verified on Hugging Face. `methodology.md`, which still described the
  dataset as future work, was folded into evaluation.md's principles.
- **GitHub templates.** A template for evaluation problems, and a PR template
  that asks for implementation notes, risks and rollback, and confirms no paid
  calls were added. The bug and feature templates referenced labels that don't
  exist (`bug`, `enhancement`), so labels were never applied; they now use the
  repository's own labels.
- **Removed:** the unused `data/processed/` placeholder.
- **Blocked:** GitHub metadata (description, topics) and closing the issues
  that are already done need API write access, which this environment doesn't
  have. 44 issues are open and none closed. The owner's action list is in the
  session report.

### Session 5 — Background ingestion (2026-10-29)

- **Owner decision:** the reranker scores 5 candidates by default, not 20
  (`RAG_RERANK_CANDIDATES`, still configurable). Re-measured on golden v2: test
  hit rate 1.000 and MRR 0.946 either way; overall MRR 0.954 against 0.958
  (one dev question, `inc-06`, needs 20); rerank p50 31 ms against 234 ms. The
  reranked and Claude gates now expect 5.
- **Background ingestion (FR-3,
  [ADR-0006](decisions/0006-background-ingestion.md)).** Uploads return `202`
  after the checks that need no parsing. Worker threads in each API process
  claim documents from the `documents` table (compare-and-set, with renewable
  leases), so the queue needs no broker and survives restarts. Uploaded bytes
  are kept in the database until the document is ready. Failures record a code
  and a message; `POST /v1/documents/{id}/retry` or an identical re-upload
  queues them again. Migration 0002 marks existing documents `ready`. New
  settings: `RAG_INGEST_WORKERS`, `RAG_INGEST_POLL_INTERVAL_S`,
  `RAG_INGEST_LEASE_S`, `RAG_INGEST_MAX_ATTEMPTS`.
- **Verified** by 23 service-level and 4 API tests: every state and
  transition, failure reasons, retry, duplicate uploads in each state, a
  crashed worker's document taken over after its lease (the late worker's
  result is discarded, including after its last renewal), repeated
  interruptions, shutdown hand-back, deletion mid-processing, restarts, two
  workers with separate database engines processing each document once, and
  migration of existing data both ways.
- **Verified with real server processes** (uvicorn, offline configuration):
  uploads queued by a server without workers were processed after a restart; a
  server hard-killed two seconds into embedding a 900-chunk document left it
  `processing` with no chunks, and the next server took it over (attempt 2)
  and finished it with exactly 900 chunks; two uvicorn worker processes
  processed 12 uploads exactly once each.
- **Fixed:** document timestamps were returned without a time zone when read
  back from SQLite; they are now always UTC (`Z`).
- **Paid-evaluation plan re-run, free.** `rag eval --plan` now also prints the
  retrieval configuration and the questions per category, naming those in two.
  Focused run: 13 questions (10 on-topic unanswerable and 4 injection;
  `t-par-06` is both), 13 calls, about 14,565 input tokens (estimate), $0.14 to
  $0.45, worst case $4.22. `--count-tokens` was refused again: the account
  still has no credit. Nothing was generated; Claude's abstention and injection
  resistance remain **unverified**.

Checks at the end of the session:

| Check | Result |
| --- | --- |
| Ruff lint and format, mypy `--strict` (src and scripts) | Pass |
| pytest (offline) | 244 passed, 97% coverage; live and paid tests skipped. The background-ingestion and API tests passed 15 runs in a row |
| Live gates, free (`thresholds.fastembed.json`, `thresholds.reranked.json` with 5 candidates) | Pass |
| Real-process checks (restart, hard kill, two uvicorn workers) | Pass |
| CI on GitHub (Python 3.12 and 3.14, gitleaks, pip-audit, markdownlint) | Pass on every push |

### Session 4 — Paid-evaluation preparation (2026-10-29)

- **Owner decisions recorded:** SQLite for v0 (ADR-0002 accepted, D-3) and
  Anthropic as the configurable hosted answer provider with the offline mode
  kept (ADR-0003 accepted, D-2).
- **`rag eval --plan`** previews a generation run. It lists the questions that
  would reach Claude, what each one tests, the estimated cost range and worst
  case, and the exact command. It never generates (tests enforce that).
  `--count-tokens` uses the free token-counting endpoint for exact input
  counts. `--split` and `--tags` select a focused run.
- **Acceptance gate** `eval/thresholds.claude.json` with requirement targets:
  on-topic unanswerable declined ≥ 0.90, injection resisted = 1.0, injection
  questions still answered, answerable questions answered ≥ 0.90. A test checks
  every metric in it resolves.
- **Verified without paid calls:**
  - The spending guard refuses `--generation` with Claude without `--allow-paid`,
    using the real `.env`.
  - The key loads from `.env` into the SDK.
  - The key never appears in logs, settings dumps, reports or plans (tested).
  - The API authenticates the key: a bad key gets 401, this one gets the billing
    check.
  - The account has **no credit balance**: every request, including free token
    counting, returns "Your credit balance is too low". The error used to read
    "rejected the request (HTTP 400)" and now says so plainly.
- **Not verifiable yet:** the exact request shape and exact token counts
  (token counting needs credit), and all generation behavior.
- **Reranker trade-offs** measured reproducibly for 5, 10 and 20 candidates
  ([evaluation.md](evaluation.md#reranker-trade-offs)): 5 candidates give the same
  test results at about a seventh of the latency.
- Reports now record each answer's stop reason and billed tokens. The paid smoke
  test targets an on-topic unanswerable question and the planted injection.

### Session 3 — Abstention and evaluation dataset (2026-10-29)

- **Paid-run guard.** `rag eval --generation` with the Claude generator refuses to
  start without `--allow-paid`, and the Claude live test needs
  `RAG_RUN_PAID_TESTS=1`, separate from free live tests. No paid call was made
  in this session.
- **Golden v2** (`eval/golden_v2.jsonl`): v1's 36 questions unchanged as the
  `dev` split, plus 58 held-out `test` questions. 18 of the 94 are unanswerable
  (10 on-topic, 8 off-topic). Three new corpus documents, one with a planted
  prompt injection and a conflicting claim. Cases are tagged, and a test checks
  every label against the corpus.
- **Evaluation:** per-split and per-tag summaries, an injection-resistance
  metric, and gates that record their dataset and can check split metrics.
  Gates recalibrated on v2, with a new gate for the reranked configuration.
- **Abstention:** an optional cross-encoder reranker
  ([ADR-0005](decisions/0005-reranking-and-abstention.md)), chosen from three
  candidates, with a rerank-score threshold calibrated on dev only. On held-out
  test questions: MRR 0.931 → 0.946, off-topic abstention margin 0.03 → 2.0, no
  false abstentions. Finding: **no retrieval signal (cosine, BM25 or
  cross-encoder) separates on-topic unanswerable questions from answerable
  ones**, so that part of abstention depends on the generator.
- Docs updated: evaluation, architecture, API, configuration, operations,
  security, requirements, roadmap.
- An empty local commit by the owner (`7953d31`, no file changes) was found on
  `main` ahead of `origin`. It was kept, not rewritten, and pushed with the
  session's work.

Checks at the end of the session:

| Check | Result |
| --- | --- |
| Ruff lint and format, mypy `--strict` (src and scripts) | Pass |
| pytest (offline) | 197 passed, 96.2% coverage (floor 85%); live and paid tests skipped |
| CI regression gate on golden v2 (`eval/thresholds.json`, hashing) | Pass: hit rate 0.947, recall 0.941, MRR 0.867; test hit rate 0.957 |
| Live gates, free (`thresholds.fastembed.json`, `thresholds.reranked.json`) | Pass |
| CI on GitHub (Python 3.12 and 3.14, gitleaks, pip-audit, markdownlint) | Pass on every push |
| Branch protection on `main` | Enabled (verified through the GitHub API); direct pushes still work |

### Session 2 — Integration and repository consolidation (2026-10-29)

- Integrated the unpushed Session 0 work (5 commits, unchanged) and the
  Session 1 backend (11 commits) on `main` with one merge commit, reconciling
  the 7 conflicting files by hand.
- ADR-0001 stays Proposed (merging isn't acceptance). The backend's storage and
  generation choices became Proposed ADR-0002 and ADR-0003.
- CI gained a secret scan, dependency audit, Markdown lint and lock check, with
  SHA-pinned third-party actions.
- Fixes, each with a regression test: an empty `RAG_API_KEY=` enabled auth
  with an empty key; `ANTHROPIC_API_KEY` in `.env` never reached the SDK; PDF
  uploads were checked by extension only; possessives produced a stray `s`
  token that matched unrelated text; validation errors didn't use the common
  error envelope; the CLI treated a directory named `x.md` as a document.
- New references: [api.md](api.md), [configuration.md](configuration.md),
  [security.md](security.md). Documentation tests check links and settings.

### Session 1 — v0 backend (2026-10-28)

- Built the backend, without access to the Session 0 plan, which existed only
  as a local bundle at the time.
- Retrieval measured for 2 embedders × 3 modes; abstention thresholds
  calibrated per embedder.

### Session 0 — Repository hygiene and Phase 0 decisions (2026-10-28)

- `.gitignore`, `.gitattributes` (LF), issue and PR templates, `SECURITY.md`,
  `.markdownlint-cli2.yaml`, fixed escaped Markdown.
- [requirements.md](requirements.md), [ADR-0001](decisions/0001-technology-stack.md)
  and an ADR [template](decisions/template.md), the target architecture (now §9
  of [architecture.md](architecture.md)) and the Phase 0–9 [roadmap](roadmap.md).

## Open decisions

| ID | Decision | Recommendation | Needed by |
| --- | --- | --- | --- |
| D-1 | How changes reach GitHub | **Resolved 2026-10-29:** sessions on the developer PC commit and push verified changes directly to `main` | — |
| D-2 | Generation model provider | **Resolved 2026-10-29:** Anthropic as the configurable hosted provider, offline mode kept ([ADR-0003](decisions/0003-generation-provider-claude.md), accepted). Live behavior is verified only once D-4's run passes | — |
| D-3 | Storage for v0 | **Resolved 2026-10-29:** SQLite for v0 ([ADR-0002](decisions/0002-v0-storage-and-scope.md), accepted); PostgreSQL + pgvector arrives with accounts in Phase 5 | — |
| D-4 | Authorize the first paid Claude evaluation. Focused run (on-topic unanswerable + injection): 13 calls, est. $0.14–0.45, worst case $4.22. Full golden v2: 86 calls, est. $0.91–2.97, worst case $27.91. Commands in [evaluation.md](evaluation.md#paid-evaluation-with-claude) | Add credit, then authorize the focused run; run the full set if it passes | Next milestone |

## Blockers and risks

| Item | Impact | Resolution |
| --- | --- | --- |
| The Anthropic account has no API credit (checked 2026-10-29) | No Claude call can succeed, including free token counting | Owner: add credits under Plans & Billing in the Claude Console |
| Paid evaluation not yet authorized | On-topic abstention, citation precision and injection resistance with Claude are unverified, and the abstention target can't be confirmed | D-4 |
| On-topic unanswerable questions get past every retrieval signal | If Claude doesn't decline them reliably, the abstention target needs an answerability model | Measure first (D-4); [ADR-0005](decisions/0005-reranking-and-abstention.md) lists the next option |
| No Docker on the PC | PostgreSQL can't run locally; not needed until Phase 5 (D-3) | Install Docker Desktop or PostgreSQL before Phase 5 |
| The dataset is small and author-written | Results are optimistic and coarse | Add questions from real usage when available |
| `Desktop\production-rag` is an older clone that contains this repository as an untracked folder | `git add .` in the outer folder would embed this repository | Work only in `Desktop\production-rag\production-rag`; move it out of the outer clone when convenient |

## Next priorities

1. Owner: add API credit, then authorize D-4 (focused run first).
2. Before the run: `rag eval --plan --count-tokens` for exact input counts.
3. Run the paid evaluation and record it in [evaluation.md](evaluation.md). If
   on-topic declines fall short, evaluate an answerability check.
4. Meanwhile (free): DOCX and HTML (FR-2), then answer streaming (FR-9).
   Before untrusted users can upload, move parsing to a separate,
   resource-limited worker process ([ADR-0006](decisions/0006-background-ingestion.md)).
5. Phase 5: accounts and isolation, with PostgreSQL + pgvector.
