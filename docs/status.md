# Project Status

Last updated: 2026-10-29

## Current state

**v0 backend: implemented and on `main`.** Ingestion (PDF, TXT, Markdown),
hybrid retrieval (BM25 + vectors with RRF) with optional cross-encoder
reranking, grounded generation with Claude search-result citations, an HTTP
API, the `rag` CLI, and an evaluation suite of 94 questions with a held-out
test split, gated in CI. See [architecture.md](architecture.md) and the
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
| Ingestion status (FR-3), streaming (FR-9), DOCX/HTML (FR-2) | Not started |

## Session log

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
| D-4 | Authorize the first paid Claude evaluation. Focused run (on-topic unanswerable + injection): 13 calls, est. $0.14–0.45, worst case $4.22. Full golden v2: 86 calls, est. $0.91–2.98, worst case $27.92. Commands in [evaluation.md](evaluation.md#paid-evaluation-with-claude) | Add credit, then authorize the focused run; run the full set if it passes | Next milestone |

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
4. Meanwhile (free): background ingestion with status reporting (FR-3), then
   DOCX and HTML (FR-2).
5. Phase 5: accounts and isolation, with PostgreSQL + pgvector.
