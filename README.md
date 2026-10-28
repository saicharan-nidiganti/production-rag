# Production RAG — Ask My Docs

A production-oriented Retrieval-Augmented Generation system designed around reliable retrieval, grounded generation, citations, evaluation, regression testing, observability, and reproducibility.

Upload your documents, ask questions in plain language, and get answers grounded in those documents. Every answer cites the exact passages it came from, and the system says so when the documents don't contain the answer.

> **Status:** Phase 0 (foundation) is complete. Requirements, architecture and the technology stack are defined. **There is no runnable code yet.** Next up is Phase 1, the platform skeleton. See [docs/status.md](docs/status.md) and the [roadmap](docs/roadmap.md).

---

## Overview

Production RAG is an engineering-focused RAG system built to explore what is required to move beyond a basic "retrieve and generate" prototype.

The project treats evaluation, observability, security, reproducibility, and regression detection as first-class parts of the system.

### Core goals

- Reliable document ingestion
- High-quality retrieval
- Hybrid retrieval and reranking
- Grounded answer generation
- Source citations
- Citation correctness evaluation
- Retrieval and generation evaluation
- Regression detection
- Performance benchmarking
- Structured observability
- Secure configuration and deployment
- Reproducible releases

---

## Architecture

The RAG lifecycle:

```text
Documents
    │
    ▼
Ingestion
    │
    ▼
Text Extraction
    │
    ▼
Chunking + Metadata
    │
    ▼
Embeddings / Indexing
    │
    ▼
┌─────────────────────┐
│ Retrieval           │
│                     │
│ • Vector Search     │
│ • Hybrid Retrieval  │
│ • Reranking         │
└──────────┬──────────┘
           │
           ▼
    Context Assembly
           │
           ▼
    Grounded Generation
           │
           ▼
     Citations
           │
           ▼
        Response
           │
           ├──────────────► Observability
           │
           └──────────────► Evaluation
                              │
                              ▼
                       Regression Gates
```

Component design, the data model, request flows and security boundaries are described in [docs/architecture.md](docs/architecture.md).

### Planned stack

The rationale and the alternatives considered are in [ADR-0001](docs/decisions/0001-technology-stack.md).

| Layer | Choice |
| --- | --- |
| API and worker | Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic |
| Storage and search | PostgreSQL + pgvector (vectors) + full-text search, fused with Reciprocal Rank Fusion |
| Parsing | pypdf / pdfplumber, python-docx, HTML and Markdown parsers; Tesseract OCR later |
| Embeddings | Local sentence-transformers model by default, behind a provider interface |
| Generation | Provider interface with streaming (provider selection pending) |
| Frontend | React, TypeScript, Vite, Tailwind CSS |
| Quality | Ruff, mypy, pytest, ESLint, Playwright, GitHub Actions, gitleaks |

---

## Documentation

| Document | Contents |
| --- | --- |
| [Requirements](docs/requirements.md) | Scope, functional and non-functional requirements, constraints, success criteria |
| [Architecture](docs/architecture.md) | Components, data model, ingestion and question-answering flows, security, evaluation |
| [Methodology](docs/methodology.md) | Evaluation-first engineering principles |
| [Decisions](docs/decisions/README.md) | Architecture Decision Records |
| [Roadmap](docs/roadmap.md) | Phase-by-phase checklist |
| [Status](docs/status.md) | Current progress, checks run, open decisions |

## Repository layout

```text
src/            Python package (added in Phase 1)
tests/          Automated tests (added in Phase 1)
frontend/       Web application (added in Phase 6)
configs/        Retrieval and evaluation configuration (added in Phase 3)
evaluations/    Versioned golden datasets (added in Phase 3)
reports/        Generated evaluation reports (added in Phase 3)
data/           Local-only working data (contents git-ignored)
docs/           Project documentation
```

## Getting started

Setup instructions will be added with the Phase 1 platform skeleton (Python environment, PostgreSQL + pgvector, and configuration through `.env`).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch naming, commit conventions, code standards and testing expectations.

## Security

Please report vulnerabilities privately. See [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
