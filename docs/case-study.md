# Case study: building a RAG service to production standards

An engineering account of the project: the problem, the decisions and their trade-offs, how
quality is measured, what testing found, and what is still open. Each claim links to the
document or code that backs it.

## The problem

Teams keep their knowledge in documents: runbooks, policies, handbooks. A retrieval-augmented
generation (RAG) service lets people ask questions of them, but the usual prototype has three
failures that matter in practice:

- **It answers confidently without evidence**, inventing what the documents don't say.
- **Nobody knows whether a change made it better or worse**, because quality isn't measured.
- **It isn't operable**: no isolation between users, no backups, no metrics, no plan for
  when it breaks.

The goal was a self-hosted service that cites the passages it used, declines when the
documents don't hold the answer, measures itself on every change, and can be run by someone
other than its author.

## What was built

| Area | What exists | Where |
| --- | --- | --- |
| Ingestion | PDF, DOCX, HTML, Markdown and text, parsed in an isolated process, queued and processed in the background, with crash recovery | [architecture.md](architecture.md#4-ingestion) |
| Retrieval | Hybrid BM25 and vector search fused with reciprocal rank fusion, optional cross-encoder reranking, abstention on weak evidence | [ADR-0004](decisions/0004-embeddings-hybrid-retrieval-and-abstention.md), [ADR-0005](decisions/0005-reranking-and-abstention.md) |
| Answers | Claude with native search-result citations, mapped back to stored passages; streamed over Server-Sent Events; an offline generator for free use | [ADR-0003](decisions/0003-generation-provider-claude.md), [ADR-0010](decisions/0010-streaming-answers-over-sse.md) |
| Users | Accounts, sessions and API tokens; every document, search and citation limited to its owner | [ADR-0012](decisions/0012-accounts-and-per-user-isolation.md) |
| Storage | SQLite for one instance, PostgreSQL with pgvector for several | [ADR-0013](decisions/0013-postgresql-with-pgvector.md) |
| Interface | A web interface served on the API's origin, under a strict Content-Security-Policy | [ADR-0011](decisions/0011-web-interface-served-by-the-backend.md) |
| Operations | Metrics, structured logs, backups that verify themselves, runbooks, a container image | [operations.md](operations.md), [runbooks.md](runbooks.md) |
| Quality | A golden dataset with a held-out split, retrieval and citation gates, a performance gate, a release checklist | [evaluation.md](evaluation.md), [release-criteria.md](release-criteria.md) |

## Decisions that shaped it

- **Abstain before generating.** If the best retrieval scores fall below calibrated
  thresholds, the service answers "not enough evidence" without calling the model. That is
  cheaper, faster, and the only reliable defence against confident answers to off-topic
  questions. The thresholds are calibrated on the development split and checked on the
  held-out split, so they aren't tuned to the test.
- **An answer that cites nothing isn't an answer.** The model receives each passage as a
  citable block, and its citations are mapped to stored passages. An answer without a valid
  citation is reported as insufficient evidence, never as answered.
- **Measure, then decide.** Reranking 5 candidates rather than 20 was chosen from measured
  quality and latency. On PostgreSQL, `ts_rank` replaced `ts_rank_cd` only after the
  evaluation showed identical quality and a 30% faster query. A guess that out-of-line
  vector storage made searches slow was tested, and was wrong.
- **Filter inside the search, not after it.** With accounts, the owner condition is part of
  both search queries. Filtering results afterwards would let a large library crowd a small
  one out of the candidates, and would leave every new query path one forgotten filter away
  from a leak.
- **Keep the default free and offline.** With no configuration, the service uses a hashing
  embedder and an extractive generator: no downloads, no network, no cost. CI runs that
  configuration, and anything paid needs an explicit flag.
- **Prefer boring infrastructure.** One process and one SQLite file serve a team. PostgreSQL
  is supported for when several instances are needed, rather than required from the start.

## How quality is measured

The golden dataset is 94 author-written questions over a synthetic corpus of nine documents,
with a held-out test split. It includes questions the documents can't answer, both on-topic
and off-topic, and a planted prompt injection. Every change runs:

| Gate | Measures | Current result |
| --- | --- | --- |
| Retrieval, offline | Hit rate, recall, MRR, no false abstention | Hit rate 0.947, MRR 0.867 on SQLite; 0.961 and 0.874 on PostgreSQL |
| Retrieval, recommended models | The same, with bge-small embeddings and reranking | Hit rate 0.987, MRR 0.954 |
| Answers and citations | Answered rate, citation precision, fact recall, with the extractive generator | 1.0, 0.579, 0.855 |
| Performance | Latency percentiles per stage, throughput and memory, at 10,000 chunks | Retrieval p95 13 ms on SQLite, 95 ms on PostgreSQL; target 300 ms |
| Claude | Declining unanswerable questions, resisting injection | Prepared, not yet run: no API credit |

The dataset is small and written by the same author as the corpus, so the numbers are
optimistic and coarse. They are useful for catching regressions, not as a claim about
quality on real documents.

## What testing found

Some of the most useful work was finding things that looked fine and weren't:

- **The isolation tests passed on their first run.** That isn't evidence they work, so the
  owner filter was broken on purpose in five places. One break went unnoticed: a second
  check when loading chunks masked a lexical search that ignored the owner. A test that
  calls each search directly now catches it.
- **SQLite ignores column lengths; PostgreSQL enforces them.** Running the suite on
  PostgreSQL found a 71-character DOCX media type in a 64-character column.
- **Two CI runs of the same code differed by 90% in latency.** The performance gate's first
  thresholds would soon have failed builds for no reason. They were reset from that
  measurement: warn at 2×, fail at 4×, which catches an algorithmic blow-up rather than
  noise.
- **A rebuild of an old release would have mixed in new files**, and later would have
  failed outright, because the container workflow built old source with a new Dockerfile.
  The first image scan also found two high-severity zlib vulnerabilities in the base image.
  All three were fixed, and the published 0.2.0 image was rebuilt.
- **A flaky test**: the release dry run caught a benchmark test that compared timings and
  peak memory between two runs in one process. It now compares only deterministic metrics.

## Engineering practice

- **Every decision with a trade-off has an ADR**: 13 of them, each with the options
  considered and when to revisit it ([decisions/](decisions/README.md)).
- **CI on every push**: lint, `mypy --strict`, about 780 tests on three Python versions with
  a 90% coverage floor, the suite on PostgreSQL, the web interface, the retrieval gates, the
  benchmark, a secret scan, a dependency audit and CodeQL.
- **Releases are checked, not asserted**: a tag runs 22 criteria and publishes only if none
  that blocks fails ([release-criteria.md](release-criteria.md)).
- **No secret in logs**: keys and the database password are redacted, and question and
  document text are never logged ([observability.md](observability.md#sensitive-data)).

## Limitations

- Claude's answer quality hasn't been measured live.
- No self-service sign-up, password reset by email or second factor.
- Rate limits are per process; searches are exact, with no approximate index yet.
- No OCR for scanned PDFs, and the parser is isolated but not sandboxed.
- The evaluation set is small, and the web interface hasn't had an accessibility audit.

The full list, with plans, is in [security.md](security.md#known-gaps) and the
[roadmap](roadmap.md).

## What's next

1. Run the paid Claude evaluation and publish its result against the acceptance gate.
2. An approximate vector index on PostgreSQL, measured against exact search.
3. A larger evaluation set written by someone other than the corpus's author.
4. Sign-up with email verification, password reset and a second factor, before exposing the
   service to people the operator doesn't know.
