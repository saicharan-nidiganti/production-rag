# ADR 0004: Embedding providers, hybrid retrieval and evaluation-calibrated abstention

- **Status:** Accepted
- **Date:** 2026-10-28

## Context

Retrieval quality depends on the embedding model, and the methodology asks for decisions backed
by measurement. Tests and CI must be deterministic and must not depend on model downloads.
Abstention needs a threshold, and cosine scores are not comparable across embedding models.

## Measurements

`rag eval` on `eval/golden_v1.jsonl` (30 answerable and 6 unanswerable questions, 6 documents,
top-k = 5):

| Embeddings | Mode | Hit rate | Recall | MRR | Retrieval p95 |
|---|---|---|---|---|---|
| hashing | lexical | 0.967 | 0.967 | 0.900 | 1.2 ms |
| hashing | vector | 0.900 | 0.883 | 0.817 | 1.1 ms |
| hashing | hybrid | 0.967 | 0.950 | 0.906 | 1.3 ms |
| fastembed bge-small | lexical | 0.967 | 0.967 | 0.900 | 1.0 ms |
| fastembed bge-small | vector | 1.000 | 1.000 | 0.934 | 6.3 ms |
| fastembed bge-small | hybrid | 1.000 | 1.000 | 0.957 | 7.5 ms |

The questions lexical retrieval misses are paraphrases ("Who is allowed to downgrade an outage's
classification?" when the text says "lower" and "severity"). Semantic embeddings fix them, and
hybrid fusion ranks the right chunk higher than either retriever alone.

## Decision

1. **Hybrid retrieval is the default mode.** It matched or beat both single retrievers with
   either embedder.
2. **Two embedding providers.** `fastembed` (local ONNX, `BAAI/bge-small-en-v1.5`) is the
   recommended production setting, justified by the table above. `hashing` (deterministic
   feature hashing, no dependencies) remains the code default, so the system and its test suite
   run anywhere with no download. The API logs a warning at startup when it runs on `hashing`.
3. **Abstention thresholds are per provider and calibrated on the dataset**, chosen so that no
   answerable question abstains, with margin:

   | Provider | Lowest answerable best-cosine | Unanswerable best-cosines | Threshold | Unanswerable abstained |
   |---|---|---|---|---|
   | hashing | 0.071 | 0.040–0.179 | 0.05 | 1 of 6 |
   | fastembed | 0.624 | 0.448–0.690 | 0.55 | 3 of 6 |

   Retrieval-level abstention only catches clearly off-topic questions. On-topic questions the
   corpus doesn't answer ("How much does the Enterprise plan cost?") score like answerable ones,
   so declining them is the generator's job (ADR 0003).
4. **A CI regression gate** (`eval/thresholds.json`) for the deterministic configuration, and a
   second gate (`eval/thresholds.fastembed.json`) for the recommended configuration, run as a
   live test. Each thresholds file records the configuration it was calibrated for, and a report
   from a different configuration fails the gate instead of being compared with numbers that
   don't apply.

## Consequences

- The dataset is small and was written together with the corpus, so absolute numbers are
  optimistic. Use them to detect regressions, not to compare with external benchmarks. Grow the
  dataset from real user questions before tuning further.
- With 36 questions, thresholds are coarse: one question changes hit rate by 3.3 points. The
  gates leave room for about one additional miss.
- Moving the default to `fastembed` would add about 5 ms per query and a model download to
  every environment, including CI. Revisit once CI caches the model.
