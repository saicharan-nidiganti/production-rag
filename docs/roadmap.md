# Roadmap

Phases are planning units. Work lands on `main` in focused commits (see
[CONTRIBUTING.md](../CONTRIBUTING.md#branches-and-commits)). Current progress,
checks run and open decisions are tracked in [status.md](status.md).

The v0 backend delivered most of Phases 1–4, and parts of 7 and 8, ahead of the
original order. Where it departs from [ADR-0001](decisions/0001-technology-stack.md),
the item notes the proposed decision.

## Phase 0 — Foundation

- [x] Define project requirements — [requirements.md](requirements.md)
- [x] Define constraints — [requirements.md](requirements.md#5-constraints)
- [x] Define success criteria — [requirements.md](requirements.md#7-success-criteria)
- [x] Evaluate architecture options — [ADR-0001](decisions/0001-technology-stack.md)
- [x] Create initial architecture decision — [ADR-0001](decisions/0001-technology-stack.md)
- [x] Establish engineering conventions — [CONTRIBUTING.md](../CONTRIBUTING.md)
- [x] Repository hygiene (`.gitignore`, line endings, issue/PR templates, security policy)

## Phase 1 — Platform Skeleton

- [x] Python project with pinned dependencies (uv), Ruff, mypy, pytest
- [x] FastAPI application with settings, structured logging and health checks
- [ ] PostgreSQL + pgvector via Docker Compose — v0 uses SQLite by owner decision ([ADR-0002](decisions/0002-v0-storage-and-scope.md)); moves to Phase 5
- [x] Initial database schema and migrations (Alembic)
- [x] `.env.example` and reproducible local setup instructions
- [x] GitHub Actions CI (lint, type-check, tests, secret scan, dependency audit) — plus the eval gate and Markdown lint; the audit also runs weekly

## Phase 2 — Document Ingestion

- [x] Define supported document types — PDF, TXT, Markdown
- [ ] DOCX and HTML support (FR-2)
- [x] Design ingestion pipeline
- [x] Implement document loading
- [x] Implement text extraction
- [x] Design chunking strategy
- [x] Implement chunking
- [x] Preserve document metadata — heading path, page, character offsets, content hash
- [x] Upload validation, duplicate detection and ingestion status reporting — background workers with per-document status, failure reasons and retry (FR-3, [ADR-0006](decisions/0006-background-ingestion.md))
- [x] Handle empty, encrypted, corrupt and oversized files
- [x] Add ingestion tests

## Phase 3 — Retrieval

- [x] Define retrieval requirements
- [x] Design retrieval interface
- [x] Implement embedding pipeline — `hashing` and `fastembed` providers
- [x] Implement vector retrieval
- [x] Implement keyword (full-text) retrieval, so hybrid retrieval can be evaluated — SQLite FTS5 BM25
- [ ] Enforce owner and metadata filters inside retrieval queries — v0 is single tenant
- [x] Create retrieval evaluation dataset — `eval/golden_v1.jsonl`
- [x] Measure retrieval quality (baseline) — [evaluation.md](evaluation.md)
- [x] Add retrieval tests

## Phase 4 — Generation

- [x] Integrate generation model — Claude, configurable, with an offline mode ([ADR-0003](decisions/0003-generation-provider-claude.md))
- [x] Design context assembly — one `search_result` block per chunk
- [x] Implement grounded generation
- [x] Implement citation generation
- [x] Verify citations against retrieved evidence — enforced structurally
- [x] Implement unsupported-answer handling — pre-generation abstention and the uncited-answer rule
- [ ] Defend against prompt injection in document content — system-prompt mitigation in place; injection cases in golden v2 (`t-par-*`), to be measured with Claude
- [x] Add generation tests
- [ ] Stream answers to the client (FR-9)
- [ ] Measure Claude answers: prepared (`rag eval --plan`, acceptance gate `eval/thresholds.claude.json`); blocked on API credit and the owner's authorization

## Phase 5 — Security and Multi-User Isolation

- [ ] User accounts and session authentication — v0 has an optional static API key
- [ ] Server-side document ownership checks on every endpoint
- [ ] Cross-tenant isolation test suite
- [ ] Rate limiting and upload hardening — size and page limits done; rate limiting pending
- [ ] Security review

## Phase 6 — Frontend

- [ ] Authentication screens
- [ ] Upload with progress and ingestion status
- [ ] Document library
- [ ] Streaming question answering with clickable citations
- [ ] Loading, empty and error states; accessibility pass

## Phase 7 — Advanced Retrieval and Evaluation

- [x] Investigate hybrid retrieval
- [x] Implement hybrid retrieval if justified — it beats either retriever alone ([ADR-0004](decisions/0004-embeddings-hybrid-retrieval-and-abstention.md))
- [x] Evaluate reranking approaches — three cross-encoders compared ([ADR-0005](decisions/0005-reranking-and-abstention.md))
- [x] Implement reranking if justified — optional; test MRR 0.931 → 0.946 and robust off-topic abstention
- [x] Compare retrieval strategies
- [x] Create golden evaluation dataset — v2: 94 questions with a held-out test split; grow it from real questions
- [x] Define evaluation metrics
- [x] Implement retrieval evaluation
- [x] Implement answer evaluation — fact-recall proxy
- [x] Implement citation evaluation — citation precision
- [x] Add regression evaluation — threshold gate in CI
- [x] Generate evaluation reports
- [ ] LLM-as-judge groundedness and answer correctness
- [ ] Abstention on on-topic unanswerable questions — no retrieval signal separates them; measure Claude's declines, then consider an answerability model

## Phase 8 — Production Readiness

- [x] Error handling
- [x] Configuration management
- [x] Logging
- [ ] Observability — JSON logs, request ids and per-stage timings done; metrics export pending
- [ ] Performance testing
- [ ] OCR for scanned PDFs
- [ ] Container images
- [x] Operational documentation — [operations.md](operations.md)
- [x] Deployment smoke test and package check — `make smoke` (a real server over HTTP) and `make package` (the installed wheel) run in CI

## Phase 9 — Release

- [ ] Final evaluation
- [ ] Architecture documentation
- [x] Methodology documentation — the principles are in [evaluation.md](evaluation.md#principles)
- [ ] README completion
- [ ] Demo preparation
- [ ] Final cleanup
- [ ] Release
