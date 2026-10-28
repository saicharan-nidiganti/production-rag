# Roadmap

Checked items are implemented on the `feature/backend-mvp` branch. Notes link to where each one
is documented.

## Phase 0 — Foundation

- [x] Define project requirements
- [x] Define constraints
- [x] Define success criteria: the retrieval regression gate in `eval/thresholds.json`
- [x] Evaluate architecture options: [ADRs 0001–0004](decisions/)
- [x] Create initial architecture decision: [ADR 0001](decisions/0001-backend-stack.md)
- [x] Establish engineering conventions: ruff, mypy `--strict`, pytest with a coverage floor, CI

## Phase 1 — Document Ingestion

- [x] Define supported document types: `.txt`, `.md`, `.pdf`
- [x] Design ingestion pipeline
- [x] Implement document loading
- [x] Implement text extraction
- [x] Design chunking strategy: section-bounded, boundary-aware, with overlap
- [x] Implement chunking
- [x] Preserve document metadata: heading path, page, offsets, content hash
- [x] Add ingestion tests

## Phase 2 — Retrieval

- [x] Define retrieval requirements
- [x] Design retrieval interface
- [x] Implement embedding pipeline: `hashing` and `fastembed`
- [x] Implement vector retrieval
- [x] Create retrieval evaluation dataset: `eval/golden_v1.jsonl`
- [x] Measure retrieval quality: [evaluation.md](evaluation.md)
- [x] Add retrieval tests

## Phase 3 — Generation

- [x] Integrate generation model: Claude, `claude-opus-5-5`
- [x] Design context assembly: one `search_result` block per chunk
- [x] Implement grounded generation
- [x] Implement citation generation
- [x] Implement unsupported-answer handling: pre-generation abstention plus the uncited-answer rule
- [x] Add generation tests
- [ ] Record live Claude evaluation results (`rag eval --generation` with `RAG_GENERATOR=anthropic`)

## Phase 4 — Advanced Retrieval

- [x] Investigate hybrid retrieval
- [x] Implement hybrid retrieval if justified: justified by MRR 0.906 → 0.957 ([ADR 0004](decisions/0004-embeddings-and-evaluation.md))
- [ ] Evaluate reranking approaches (needs a harder dataset: recall@5 is already 1.0)
- [ ] Implement reranking if justified
- [x] Compare retrieval strategies

## Phase 5 — Evaluation

- [x] Create golden evaluation dataset (v1: 36 questions; grow it from real questions)
- [x] Define evaluation metrics
- [x] Implement retrieval evaluation
- [x] Implement answer evaluation: fact recall proxy
- [x] Implement citation evaluation: citation precision
- [x] Add regression evaluation: threshold gate in CI
- [x] Generate evaluation reports
- [ ] LLM-as-judge faithfulness scoring

## Phase 6 — Production Readiness

- [x] Error handling
- [x] Configuration management
- [x] Logging
- [ ] Observability: metrics export and tracing (JSON logs and timings are done)
- [ ] Performance testing under concurrent load
- [ ] Security review
- [x] Operational documentation: [operations.md](operations.md)

## Phase 7 — Release

- [ ] Final evaluation
- [x] Architecture documentation: [architecture.md](architecture.md)
- [x] Methodology documentation
- [x] README completion
- [ ] Demo preparation
- [ ] Final cleanup
- [ ] Release
