# Project Status

Last updated: 2026-10-29

## Current state

**v0 backend: implemented and on `main`.** Ingestion (PDF, TXT, Markdown),
hybrid retrieval (BM25 + vectors with RRF), grounded generation with Claude
search-result citations, an HTTP API, the `rag` CLI and an evaluation suite with
a CI regression gate. See [architecture.md](architecture.md) and the
[roadmap](roadmap.md).

**Next:** decide D-2 and D-3 (below), measure Claude answers, then background
ingestion with status reporting (FR-3), DOCX/HTML (FR-2) and accounts (Phase 5).

## Requirements snapshot

| Area | State |
| --- | --- |
| Retrieval targets (Recall@5 ≥ 0.85, MRR ≥ 0.70) | Met on golden v1 with both embedders (recall 0.95 / 1.00, MRR 0.906 / 0.957) |
| Citation validity 100% | Enforced in code: citations map to retrieved chunks by index |
| False abstention ≤ 0.10 | Met: 0.00 at retrieval level |
| Correct abstention on unanswerable ≥ 0.90 | **Not met by the offline baseline**: the extractive generator answers 4 of 6 unanswerable questions; Claude not yet measured |
| Accounts and isolation (FR-1, NFR-1) | Not started; v0 is single tenant ([ADR-0002](decisions/0002-v0-storage-and-scope.md)) |
| Ingestion status (FR-3), streaming (FR-9), DOCX/HTML (FR-2) | Not started |

## Session log

### Session 2 — Integration and repository consolidation (2026-10-29)

- Integrated the unpushed Session 0 work (5 commits, unchanged) and the
  Session 1 backend (11 commits) on `main` with one merge commit. All 7
  conflicting files were reconciled by hand.
- ADRs renumbered around Session 0's ADR-0001. The backend's storage and
  generation choices are recorded as **Proposed** ADR-0002 and ADR-0003 for the
  owner's decision. ADR-0001 stays Proposed: merging it isn't acceptance.
- Architecture, README, CONTRIBUTING and the roadmap now describe the code as
  it is, with the v1 target kept as a labelled section.

### Session 1 — v0 backend (2026-10-28)

- Built the backend described above, without access to the Session 0 plan:
  that plan existed only as a local bundle and the session didn't find it.
- 124 offline tests, 96% coverage, Ruff, mypy `--strict`, verified on Python
  3.12 and 3.14. Real server smoke-tested over HTTP, including with 2 workers.
- Retrieval measured for 2 embedders × 3 modes; abstention thresholds
  calibrated per embedder ([evaluation.md](evaluation.md)).

### Session 0 — Repository hygiene and Phase 0 decisions (2026-10-28)

- `.gitignore`, `.gitattributes` (LF), issue and PR templates, `SECURITY.md`,
  `.markdownlint-cli2.yaml`, fixed escaped Markdown.
- [requirements.md](requirements.md), [ADR-0001](decisions/0001-technology-stack.md)
  (technology stack) and an ADR [template](decisions/template.md), the target
  architecture (now §9 of [architecture.md](architecture.md)) and the Phase 0–9
  [roadmap](roadmap.md).

## Open decisions

| ID | Decision | Recommendation | Needed by |
| --- | --- | --- | --- |
| D-1 | How changes reach GitHub | **Resolved 2026-10-29:** the owner authorized sessions on the developer PC to commit and push verified changes directly to `main` | — |
| D-2 | Generation model provider | Accept [ADR-0003](decisions/0003-generation-provider-claude.md) (Claude, implemented), after measuring it with `rag eval --generation` | Before production use |
| D-3 | Storage for v0: SQLite as built, or PostgreSQL now (needs Docker Desktop or a native install on Windows) | Accept [ADR-0002](decisions/0002-v0-storage-and-scope.md) for v0; move to PostgreSQL + pgvector together with accounts in Phase 5 | Before Phase 5 |

## Owner actions

These need GitHub admin access, which sessions on the PC don't have (no `gh`
CLI or token):

1. **Protect `main` without blocking direct pushes:** Settings → Rules →
   Rulesets → New branch ruleset → target `main` → enable "Block force pushes"
   and "Restrict deletions". Don't enable "Require a pull request", which would
   block the direct-to-`main` workflow.
2. **Enable private vulnerability reporting:** Settings → Code security →
   Private vulnerability reporting. [SECURITY.md](../SECURITY.md) points
   reporters there.
3. **Add an `ANTHROPIC_API_KEY`** to `.env` on the PC so a session can run
   the first live Claude evaluation (it costs cents).

## Blockers and risks

| Item | Impact | Resolution |
| --- | --- | --- |
| No Claude API credentials on the PC | Answer quality, citation precision and abstention with Claude are unmeasured | Owner action 3 |
| No Docker on the PC | PostgreSQL can't run locally | D-3 |
| The offline extractive baseline answers most unanswerable questions | Not a production generator; misleading if used as one | Use `RAG_GENERATOR=anthropic`; the extractive generator is for tests and keyless development |
| `Desktop\production-rag` is an older clone that contains this repository as an untracked folder | `git add .` in the outer folder would embed this repository | Work only in `Desktop\production-rag\production-rag`; move it out of the outer clone when convenient |

## Next priorities

1. Owner: decide D-2 and D-3, and do the owner actions.
2. Measure Claude answers and record them in [evaluation.md](evaluation.md).
3. Background ingestion with status reporting (FR-3), then DOCX and HTML (FR-2).
4. Phase 5: accounts and isolation, on PostgreSQL if D-3 is decided that way.
