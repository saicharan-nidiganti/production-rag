# ADR-0004: Embedding providers, hybrid retrieval and calibrated abstention

- **Status:** Accepted, 2026-10-29. An engineering decision within
  [ADR-0001](0001-technology-stack.md) §3 and §6, backed by measurement.
- **Date:** 2026-10-29
- **Deciders:** Engineering, from evaluation results
- **Supersedes:** none

## Context

ADR-0001 chose a local `BAAI/bge-small-en-v1.5` embedding model behind a
provider interface, and RRF fusion with `k = 60`. Retrieval quality depends on
the embedding model, and the methodology asks for decisions backed by
measurement. Tests and CI must be deterministic and must not depend on model
downloads. Abstention needs a threshold, and cosine scores are not comparable
across embedding models.

## Decision drivers

1. Measured retrieval quality on the golden dataset
2. Deterministic, download-free tests and CI
3. False abstention on answerable questions ≤ 0.10 (requirements §7)

## Options considered

`rag eval` on `eval/golden_v1.jsonl` (30 answerable and 6 unanswerable
questions over 6 documents, top-k = 5):

| Embeddings | Mode | Hit rate | Recall | MRR | Retrieval p95 |
| --- | --- | --- | --- | --- | --- |
| hashing | lexical | 0.967 | 0.967 | 0.900 | 1.2 ms |
| hashing | vector | 0.900 | 0.883 | 0.817 | 1.1 ms |
| hashing | hybrid | 0.967 | 0.950 | 0.906 | 1.3 ms |
| fastembed bge-small | lexical | 0.967 | 0.967 | 0.900 | 1.0 ms |
| fastembed bge-small | vector | 1.000 | 1.000 | 0.934 | 6.3 ms |
| fastembed bge-small | hybrid | 1.000 | 1.000 | 0.957 | 7.5 ms |

The questions lexical retrieval misses are paraphrases ("Who is allowed to
downgrade an outage's classification?" when the text says "lower" and
"severity"). Semantic embeddings fix them, and hybrid fusion ranks the right
chunk higher than either retriever alone.

## Decision

1. **Hybrid retrieval (RRF, `k = 60`) is the default mode.** It matched or beat
   both single retrievers with either embedder.
2. **Two embedding providers behind one interface.**
   - `fastembed` runs ADR-0001's `BAAI/bge-small-en-v1.5` through ONNX Runtime
     rather than sentence-transformers: same model, no PyTorch dependency. It is
     the recommended production setting.
   - `hashing`, deterministic feature hashing with no dependencies and no
     download, remains the code default. The system and its test suite therefore
     run anywhere. The API logs a warning at startup when it runs on `hashing`.
3. **Abstention thresholds are per provider and calibrated on the dataset**, so
   that no answerable question abstains, with margin:

   | Provider | Lowest answerable best-cosine | Unanswerable best-cosines | Threshold | Unanswerable abstained |
   | --- | --- | --- | --- | --- |
   | hashing | 0.071 | 0.040–0.179 | 0.05 | 1 of 6 |
   | fastembed | 0.624 | 0.448–0.690 | 0.55 | 3 of 6 |

4. **Regression gates.** `eval/thresholds.json` covers the deterministic
   configuration and runs in CI. `eval/thresholds.fastembed.json` covers the
   recommended configuration and runs as a live test. Each file records the
   configuration it was calibrated for, and a report from a different
   configuration fails the gate.

## Consequences

### Positive

- Retrieval meets the requirements targets (Recall@5 ≥ 0.85, MRR ≥ 0.70) with
  either embedder on golden v1.
- Every chunk records its embedding model. Changing models is an explicit
  `rag reindex`.

### Negative and risks

- Retrieval-level abstention only catches clearly off-topic questions. On-topic
  questions the corpus doesn't answer ("How much does the Enterprise plan
  cost?") score like answerable ones, so declining them is the generator's job
  ([ADR-0003](0003-generation-provider-claude.md)). In lexical-only mode there is
  no calibrated score, and the system abstains only when nothing matches.
- The dataset is small and was written together with the corpus, so the
  numbers are optimistic. With 36 questions, one question moves hit rate by 3.3
  points.

## Revisit when

- The golden dataset grows (from real questions), or reranking (Phase 7) is
  evaluated.
- CI caches the fastembed model. The code default could then move to
  `fastembed`.

## Amendment, 2026-10-29: tokenizer fix

The measurements above included a tokenizer bug: possessives and contractions
("what's", "customer's") produced a stray `s` token that matched unrelated text.
After the fix, re-measured with `scripts/compare_configs.py`:

| Embeddings | Mode | Hit rate | Recall | MRR |
| --- | --- | --- | --- | --- |
| hashing | vector | 0.900 | 0.883 | 0.792 (was 0.817) |
| fastembed bge-small | hybrid | 0.967 (was 1.000) | 0.967 (was 1.000) | 0.950 (was 0.957) |

All other rows are unchanged. With fastembed, vector-only now has the higher
hit rate (1.000) while hybrid keeps the best MRR (0.950 against 0.934). The
question hybrid now misses (`inc-06`, a paraphrase) sits at rank 6. The
decision stands: hybrid stays the default for its ranking quality and for exact
terms such as command names, and the difference is one question on a small
dataset. The abstention calibration is unchanged.
