# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs).

ADRs document important technical decisions, including:

- The problem being solved
- Context
- Options considered
- Trade-offs
- Decision
- Consequences

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-backend-stack.md) | Backend language, framework and project layout | Accepted |
| [0002](0002-storage.md) | SQLite with FTS5 and exact in-memory vector search | Accepted |
| [0003](0003-grounded-generation.md) | Grounded generation with Claude search-result citations | Accepted |
| [0004](0004-embeddings-and-evaluation.md) | Embedding providers, hybrid retrieval and evaluation-calibrated abstention | Accepted |

New ADRs take the next number. An ADR is not edited after acceptance except to change its
status. A changed decision gets a new ADR that supersedes the old one.
