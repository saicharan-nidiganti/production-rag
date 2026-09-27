# Production RAG

A production-oriented Retrieval-Augmented Generation system designed around reliable retrieval, grounded generation, citations, evaluation, regression testing, observability, and reproducibility.

> **Status:** Architecture and implementation in progress.

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

The final architecture will be selected based on the project's requirements and engineering constraints rather than choosing technologies prematurely.

The planned RAG lifecycle is:

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
