# Evaluation

How retrieval and answer quality are measured, and how regressions are caught. The principles
come from [methodology.md](methodology.md).

## Running it

```bash
uv run rag eval                                               # retrieval metrics
uv run rag eval --generation                                  # plus answer metrics, with RAG_GENERATOR
uv run rag eval --thresholds eval/thresholds.json             # exit 1 if the gate fails (CI)
uv run rag eval --output eval/reports                         # write the full JSON report
uv run rag eval --mode lexical --top-k 3                      # override retrieval settings
RAG_EMBEDDING_PROVIDER=fastembed uv run rag eval --thresholds eval/thresholds.fastembed.json
```

Every run builds a **fresh temporary index** from `eval/corpus/` with the current settings. A
result therefore depends only on the corpus, dataset, configuration and code, never on what
happens to be in a local database. Two runs with the same inputs produce identical retrieval
metrics (this is tested).

## Inputs

- **Corpus** (`eval/corpus/`): six short handbook documents for a fictional platform. The
  content is invented, so a model can't answer from prior knowledge, which makes grounding
  measurable. *Source:* synthetic, written for this project in October 2026. *License:* MIT,
  like the rest of the repository.
- **Golden dataset** (`eval/golden_v1.jsonl`): 36 questions. 30 are answerable and labelled
  with their relevant document and section; 6 are unanswerable from the corpus. Some of the
  unanswerable ones are off-topic and some are on-topic but missing ("How much does the
  Enterprise plan cost?"). Several answerable questions deliberately paraphrase the text, to
  test semantic retrieval.

```json
{"id": "dep-01",
 "question": "How long does a canary run before the rollout continues?",
 "relevant": [{"source": "deployment-runbook.md", "heading": "Canary releases"}],
 "must_include": ["30 minutes"]}
```

Relevance is labelled by *document and section heading*, not chunk id, so labels survive changes
to chunking. A chunk is relevant if it comes from that document and the heading is one
component of its heading path.

The dataset is versioned by filename. Change questions by adding `golden_v2.jsonl`, not by
editing v1, so earlier reports stay comparable. Each report records the dataset's SHA-256 and a
fingerprint of the corpus.

## Metrics

Retrieval metrics are computed over answerable questions, at `top_k`:

| Metric | Definition |
| --- | --- |
| `retrieval.hit_rate` | Share of questions with at least one relevant chunk retrieved |
| `retrieval.recall` | Mean share of a question's relevant sections covered by the retrieved chunks |
| `retrieval.mrr` | Mean of 1 / rank of the first relevant chunk (0 if none) |
| `retrieval.precision` | Mean share of retrieved chunks that are relevant. Bounded by about 1/k here, since most questions have one relevant section |

Abstention is retrieval-level, before any model call:

| Metric | Definition |
| --- | --- |
| `abstention.false_abstention_rate` | Share of answerable questions the system refused to attempt (should be 0) |
| `abstention.unanswerable_abstention_rate` | Share of unanswerable questions caught at retrieval |

Generation metrics (with `--generation`) use the configured generator:

| Metric | Definition |
| --- | --- |
| `generation.answered_rate` | Share of answerable questions with status `answered` |
| `generation.unanswerable_declined_rate` | Share of unanswerable questions *not* answered (abstained, declined or refused) |
| `generation.citation_precision` | Mean share of an answer's citations that point at a relevant section |
| `generation.fact_recall` | Mean share of `must_include` facts present in the answer (case-insensitive substring). A cheap proxy for correctness that can't credit paraphrases |

Latency: `latency_ms.retrieval_p50/p95` and `generation_p50/p95` (nearest-rank percentiles).

## Current results

Retrieval, by embedder and mode, with abstention disabled to show raw retrieval quality:

| Embeddings | Mode | Hit rate | Recall | MRR | p95 |
| --- | --- | --- | --- | --- | --- |
| hashing | lexical | 0.967 | 0.967 | 0.900 | 1.2 ms |
| hashing | vector | 0.900 | 0.883 | 0.817 | 1.1 ms |
| hashing | hybrid | 0.967 | 0.950 | 0.906 | 1.3 ms |
| fastembed bge-small | lexical | 0.967 | 0.967 | 0.900 | 1.0 ms |
| fastembed bge-small | vector | 1.000 | 1.000 | 0.934 | 6.3 ms |
| fastembed bge-small | hybrid | 1.000 | 1.000 | 0.957 | 7.5 ms |

Full runs with default thresholds, hybrid mode and the extractive generator:

| Metric | hashing | fastembed |
| --- | --- | --- |
| false abstention | 0.000 | 0.000 |
| unanswerable abstained at retrieval | 0.167 | 0.500 |
| answered rate (extractive) | 1.000 | 1.000 |
| unanswerable declined (extractive) | 0.333 | 0.500 |
| citation precision (extractive) | 0.633 | 0.622 |
| fact recall (extractive) | 0.833 | 0.867 |

The extractive generator quotes up to three sentences from the top chunks, so some of its
citations land on retrieved but irrelevant chunks, which caps its citation precision. It is a
floor that an LLM generator must beat. **Claude generation has not been measured yet**: the
environment where this was built had no API credentials. Run
`RAG_GENERATOR=anthropic uv run rag eval --generation --output eval/reports` to produce the
first numbers.

## Calibrating abstention

`RAG_MIN_VECTOR_SCORE` (unset means the per-provider default) is the cosine below which the
system abstains without calling the model. The defaults were chosen from the score
distributions in the full report (`per_query[].retrieval.best_vector_score`):

- **hashing**: answerable questions score 0.071 at the lowest; unanswerable ones 0.040–0.179.
  The ranges overlap, so the threshold (0.05) catches only the clearly off-topic case.
- **fastembed**: answerable questions score 0.624 at the lowest; unanswerable ones 0.448–0.690.
  The threshold (0.55) catches half, with about 0.07 margin below the lowest answerable score.

Always set the threshold so that `false_abstention_rate` stays 0: refusing an answerable question
is worse than passing an unanswerable one to a generator that can decline it. Recalibrate on your
own data, with a dataset large enough that one question doesn't move the result by several
points.

## Regression gate

`eval/thresholds.json` (deterministic configuration, run in CI) and
`eval/thresholds.fastembed.json` (recommended configuration, run as a live test) set minimum or
maximum values for summary metrics, using dotted paths such as `retrieval.mrr`. Each file also
records the configuration it was calibrated for. A report from a different configuration fails
the gate rather than being compared with numbers that don't apply to it. When a change improves
the metrics, raise the thresholds in the same pull request.

## Reports

`--output DIR` writes one JSON file per run containing: schema version, timestamp, code version
and git commit, dataset path and hash, corpus fingerprint and counts, the configuration
snapshot, the summary, and per-question raw results (retrieved sources, ranks, scores, answers,
citations, latencies). Reports are gitignored. Commit one deliberately with `git add -f` when it
documents a decision.

## Limitations

- 36 questions over 6 short documents written together with the questions. Numbers are
  optimistic and coarse, so use them for regressions, not for claims about real-world quality.
- Fact recall is substring matching, so it under-credits correct paraphrases.
- No LLM-as-judge for faithfulness yet (roadmap Phase 5). Citation precision checks that a
  citation points at the right section, not that the sentence is entailed by it.
