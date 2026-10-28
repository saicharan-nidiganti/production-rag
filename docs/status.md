# Project Status

Last updated: 2026-10-28

## Current phase

**Phase 0 — Foundation: complete** (branch `docs/session-0-foundation`, pending review and merge).
**Next:** Phase 1 — Platform skeleton.

There is no runnable application code yet.

## Session log

### Session 0 — Repository hygiene and Phase 0 decisions (2026-10-28)

Completed:

- `.gitignore` for secrets, local document data, Python/Node output, caches,
  logs and editor files. The repository is public, and `.gitignore` was
  previously empty.
- `.gitattributes`: LF line endings in the repository; binary document formats
  marked as binary. `README.md` was renormalized from CRLF.
- Fixed escaped Markdown (`\#`, `\-`, `\[ ]`) in `roadmap.md`,
  `methodology.md` and `CONTRIBUTING.md`, which rendered as literal text on
  GitHub.
- Completed the truncated `CONTRIBUTING.md`: branches, Conventional Commits,
  pull requests, code standards, testing expectations, ADR process, security
  rules.
- Fixed the README's unclosed code fence and added status, stack,
  documentation index, layout, security and license sections.
- Issue templates (bug, feature), issue config, pull request template,
  `SECURITY.md`.
- [requirements.md](requirements.md): scope, functional and non-functional
  requirements, constraints, out-of-scope items, initial success criteria.
- [ADR-0001](decisions/0001-technology-stack.md) (technology stack) and an ADR
  [template](decisions/template.md).
- [architecture.md](architecture.md): components, data model, ingestion and
  question-answering flows, citation verification, security boundaries,
  observability, evaluation.
- [roadmap.md](roadmap.md) restructured into Phases 0–9, aligned with the
  session plan. All original items are preserved, and Phase 0 is ticked.
- `.markdownlint-cli2.yaml` for consistent Markdown linting.

Checks run:

| Check | Result |
| --- | --- |
| markdownlint-cli2 v0.23.2 over all Markdown files | ✅ 0 issues |
| Relative links and anchors across all Markdown files | ✅ all resolve |
| Mermaid diagrams in `architecture.md` rendered with mermaid-cli 11.16 | ✅ 4/4 render |
| `git check-ignore` rules (`.env*` ignored, `.env.example` kept, `data/raw/*` ignored, `.gitkeep` kept, `src/**/models/` not ignored) | ✅ as intended |
| Secret pattern scan over full branch history | ✅ no findings (regex scan; gitleaks is added to CI in Phase 1) |
| Line endings: no CRLF in committed text files | ✅ |

Not run (no code exists yet): unit tests, type checks, builds.

## Open decisions

| ID | Decision | Recommendation | Needed by |
| --- | --- | --- | --- |
| D-1 | How changes reach GitHub | Install the Claude GitHub App on **this repository only**, so sessions can push feature branches for review. Until then, branches are delivered as git bundles and pushed from the developer PC. | Session 1 |
| D-2 | Generation model provider | Free local embeddings plus a paid LLM API for answers, behind provider interfaces | Phase 4 |
| D-3 | Local PostgreSQL on Windows | Docker Desktop (matches CI) | Phase 1 |

## Blockers and risks

| Item | Impact | Resolution |
| --- | --- | --- |
| The cloud workspace cannot push: the repository is not attached with credentials (read-only public access) | Changes have to be pushed manually | D-1 |
| The cloud workspace cannot reach PyPI or npm (the egress proxy denies the registries) | Phase 1 cannot install FastAPI, SQLAlchemy, Alembic, psycopg or other dependencies, or run their tests | Set the network access for this project's cloud environment to **Trusted** (package registries) |
| The PC-linked workspace has no outbound network, and cannot delete files in the connected folder | It cannot run `git fetch`/`push` or package installs there, and git commits there would leave lock files | Use it only to transfer files. Git operations run on the PC itself. |
| No PostgreSQL `pgvector` extension in the cloud workspace | Vector search cannot be integration-tested locally without installing it | Install via a package registry (once network access allows) or run the `pgvector/pgvector` Docker image |

## Next priorities

1. Review and merge `docs/session-0-foundation`.
2. Resolve D-1 and the package-registry network access, then start Session 1.
3. Session 1: Python project (uv), FastAPI skeleton, settings, structured
   logging, health checks, Docker Compose (Postgres + pgvector), Alembic
   schema, CI (lint, types, tests, gitleaks, dependency audit).
