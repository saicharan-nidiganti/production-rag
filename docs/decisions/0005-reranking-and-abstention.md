# ADR-0005: Cross-encoder reranking and rerank-score abstention

- **Status:** Accepted, 2026-10-29. An engineering decision within
  [ADR-0001](0001-technology-stack.md) §7 (evaluate a local cross-encoder; adopt
  it only if the gain justifies the latency), backed by measurement.
- **Date:** 2026-10-29
- **Deciders:** Engineering, from evaluation results
- **Supersedes:** none. It extends
  [ADR-0004](0004-embeddings-hybrid-retrieval-and-abstention.md)'s abstention
  with a second signal.
- **Amended 2026-10-29:** the default `RAG_RERANK_CANDIDATES` is now 5, not 20
  (owner decision). On golden v2's held-out test split, 5 gives the same hit rate
  and MRR as 20 at about a seventh of the latency; 20 recovers one dev question
  ([evaluation.md](../evaluation.md#reranker-trade-offs)).

## Context

Retrieval-level abstention used the best bi-encoder cosine. On golden v1 that
caught some off-topic questions, but the margin was thin, and no setting caught
on-topic questions the corpus can't answer. Golden v2 (94 questions, of which 18
are unanswerable) adds a held-out test split, so a threshold can be calibrated on
dev and checked on unseen questions.

## Decision drivers

1. Correct abstention without false abstentions (requirements §7: false
   abstention ≤ 0.10, correct abstention ≥ 0.90)
2. Thresholds that generalize from dev to test
3. Retrieval quality, at a latency the requirements allow (the 300 ms retrieval
   target excludes reranking)
4. Free and local; permissive license

## Options considered

Probed on golden v1 (fastembed hybrid retrieval, 20 candidates reranked):

| Option | Off-topic unanswerable (max score) | Answerable (min) | MRR@5 | Rerank time per query |
| --- | --- | --- | --- | --- |
| **`Xenova/ms-marco-MiniLM-L-6-v2`** (80 MB, Apache-2.0) | −9.88 | −6.84 | 0.978 | 238 ms |
| `Xenova/ms-marco-MiniLM-L-12-v2` (120 MB, Apache-2.0) | −9.03 | −6.90 | 0.978 | 449 ms |
| `jinaai/jina-reranker-v1-tiny-en` (130 MB, Apache-2.0) | −0.85 | −0.37 | 0.967 | 198 ms |
| No reranker (cosine only) | 0.595 | 0.624 | 0.950 | — |

`BAAI/bge-reranker-base` (1 GB) was not tried because of its size.
`jina-reranker-v2` is excluded by its CC-BY-NC license.

## Decision

1. An optional reranking stage (`RAG_RERANKER=fastembed`). Fused candidates
   (`RAG_RERANK_CANDIDATES`, default 20) are scored by
   `Xenova/ms-marco-MiniLM-L-6-v2`, re-sorted, and cut to `top_k`. It matches
   MiniLM-L-12's ranking with a wider separation gap, at half the latency.
2. **Rerank-score abstention:** with a reranker, the system also abstains when the
   best rerank score is below `RAG_MIN_RERANK_SCORE`. The default, −8.4, is the
   midpoint of the dev-split gap between the lowest answerable score (−6.84) and
   the highest off-topic unanswerable score (−9.88). The cosine threshold still
   applies, and either one triggers abstention. A model without a calibrated
   default never abstains on rerank score.
3. The code default stays `none`, so tests and CI need no model download. The
   recommended production configuration (`.env.example`) turns reranking on.
   `eval/thresholds.reranked.json` gates it as a live test.

## Consequences

Measured on golden v2 with `scripts/compare_configs.py` (fastembed embeddings,
hybrid retrieval):

| | Test hit rate | Test MRR | Dev MRR | Test off-topic abstained | Test on-topic abstained | False abstention |
| --- | --- | --- | --- | --- | --- | --- |
| Without reranker | 1.000 | 0.931 | 0.933 | 4 of 4 | 0 of 8 | 0 |
| With reranker | 1.000 | 0.946 | 0.978 | 4 of 4 | 0 of 8 | 0 |

### Positive

- Better ranking on held-out questions (MRR 0.931 → 0.946), and every dev
  question now has a relevant chunk in the top 5, including the paraphrase
  `inc-06`.
- Off-topic abstention is robust. On test, the highest off-topic score is
  −10.41, 2.0 below the threshold. With cosine, the closest off-topic test
  question sat 0.03 below its threshold.
- The threshold calibrated on dev produced no false abstentions on test. The
  closest answerable test question scored −7.99.

### Negative and risks

- **It doesn't solve on-topic unanswerable questions.** A relevance model scores a
  passage about the Enterprise plan as relevant to "How much does the Enterprise
  plan cost?" even though the passage doesn't answer it. Test on-topic scores
  (−4.76 to 7.73) lie inside the answerable range. Declining those remains the
  generator's job ([ADR-0003](0003-generation-provider-claude.md)). Measuring
  that needs a paid Claude evaluation, which awaits the owner's authorization.
- About 250 ms per query on a laptop CPU for 20 candidates (p95 266 ms). Five
  candidates cut that about 8×, at the cost of one dev paraphrase question.
- An 80 MB model download, and one more model to pin and re-calibrate.

## Revisit when

- The paid Claude evaluation shows how often the generator declines on-topic
  unanswerable questions. If it's below target, consider an answerability model
  (for example an extractive QA model with a no-answer option), evaluated the
  same way.
- The dataset grows enough to tune `RAG_RERANK_CANDIDATES` against latency
  budgets.
