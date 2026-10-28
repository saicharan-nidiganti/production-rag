# Roadmap

Each phase maps to one working session and lands as one or more pull requests.
Current progress, checks run and open decisions are tracked in [status.md](status.md).

## Phase 0 — Foundation

- [x] Define project requirements — [requirements.md](requirements.md)
- [x] Define constraints — [requirements.md](requirements.md#5-constraints)
- [x] Define success criteria — [requirements.md](requirements.md#7-success-criteria)
- [x] Evaluate architecture options — [ADR-0001](decisions/0001-technology-stack.md)
- [x] Create initial architecture decision — [ADR-0001](decisions/0001-technology-stack.md)
- [x] Establish engineering conventions — [CONTRIBUTING.md](../CONTRIBUTING.md)
- [x] Repository hygiene (`.gitignore`, line endings, issue/PR templates, security policy)

## Phase 1 — Platform Skeleton

- [ ] Python project with pinned dependencies (uv), Ruff, mypy, pytest
- [ ] FastAPI application with settings, structured logging and health checks
- [ ] PostgreSQL + pgvector via Docker Compose
- [ ] Initial database schema and migrations (Alembic)
- [ ] `.env.example` and reproducible local setup instructions
- [ ] GitHub Actions CI (lint, type-check, tests, secret scan, dependency audit)

## Phase 2 — Document Ingestion

- [ ] Define supported document types
- [ ] Design ingestion pipeline
- [ ] Implement document loading
- [ ] Implement text extraction
- [ ] Design chunking strategy
- [ ] Implement chunking
- [ ] Preserve document metadata
- [ ] Upload validation, duplicate detection and ingestion status reporting
- [ ] Handle empty, encrypted, corrupt and oversized files
- [ ] Add ingestion tests

## Phase 3 — Retrieval

- [ ] Define retrieval requirements
- [ ] Design retrieval interface
- [ ] Implement embedding pipeline
- [ ] Implement vector retrieval
- [ ] Implement keyword (full-text) retrieval, so hybrid retrieval can be evaluated
- [ ] Enforce owner and metadata filters inside retrieval queries
- [ ] Create retrieval evaluation dataset
- [ ] Measure retrieval quality (baseline)
- [ ] Add retrieval tests

## Phase 4 — Generation

- [ ] Integrate generation model
- [ ] Design context assembly
- [ ] Implement grounded generation
- [ ] Implement citation generation
- [ ] Verify citations against retrieved evidence
- [ ] Implement unsupported-answer handling
- [ ] Defend against prompt injection in document content
- [ ] Add generation tests

## Phase 5 — Security and Multi-User Isolation

- [ ] User accounts and session authentication
- [ ] Server-side document ownership checks on every endpoint
- [ ] Cross-tenant isolation test suite
- [ ] Rate limiting and upload hardening
- [ ] Security review

## Phase 6 — Frontend

- [ ] Authentication screens
- [ ] Upload with progress and ingestion status
- [ ] Document library
- [ ] Streaming question answering with clickable citations
- [ ] Loading, empty and error states; accessibility pass

## Phase 7 — Advanced Retrieval and Evaluation

- [ ] Investigate hybrid retrieval
- [ ] Implement hybrid retrieval if justified
- [ ] Evaluate reranking approaches
- [ ] Implement reranking if justified
- [ ] Compare retrieval strategies
- [ ] Create golden evaluation dataset
- [ ] Define evaluation metrics
- [ ] Implement retrieval evaluation
- [ ] Implement answer evaluation
- [ ] Implement citation evaluation
- [ ] Add regression evaluation
- [ ] Generate evaluation reports

## Phase 8 — Production Readiness

- [ ] Error handling
- [ ] Configuration management
- [ ] Logging
- [ ] Observability
- [ ] Performance testing
- [ ] OCR for scanned PDFs
- [ ] Container images
- [ ] Operational documentation

## Phase 9 — Release

- [ ] Final evaluation
- [ ] Architecture documentation
- [ ] Methodology documentation
- [ ] README completion
- [ ] Demo preparation
- [ ] Final cleanup
- [ ] Release
