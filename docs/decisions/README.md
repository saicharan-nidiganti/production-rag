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
| --- | --- | --- |
| [0001](0001-technology-stack.md) | Technology stack for v1 | Proposed (v1 target; §2, §8–§10 settled for v0 by 0002 and 0003; Python floor amended to 3.12) |
| [0002](0002-v0-storage-and-scope.md) | v0 storage and scope: SQLite, single tenant, synchronous ingestion | Accepted (owner, D-3); synchronous ingestion superseded by 0006 |
| [0003](0003-generation-provider-claude.md) | Generation provider: Claude with search-result citations | Accepted (owner, D-2); live behavior not yet verified |
| [0004](0004-embeddings-hybrid-retrieval-and-abstention.md) | Embedding providers, hybrid retrieval and calibrated abstention | Accepted |
| [0005](0005-reranking-and-abstention.md) | Cross-encoder reranking and rerank-score abstention | Accepted; default candidates amended to 5 |
| [0006](0006-background-ingestion.md) | Background ingestion on a SQLite-backed queue | Accepted |
| [0007](0007-isolated-document-parsing.md) | Parse documents in a separate, resource-limited process | Accepted |
| [0008](0008-architecture-style.md) | Architecture style: a local-first modular monolith behind an API and a CLI | Accepted (owner, issue #2) |

Open decisions that these ADRs depend on are tracked in
[status.md](../status.md#open-decisions).

## Process

1. Copy [template.md](template.md) to `NNNN-short-title.md`, using the next number.
2. Commit it with the change that introduces the decision.
3. An ADR that needs the project owner's decision stays **Proposed** until the
   owner accepts it. Being merged to `main` does not make it Accepted. The
   acceptance date is recorded in the status line.
4. Once accepted, an ADR is not rewritten. A later decision supersedes it with a
   new ADR, and the old one's status is updated to point to it.
