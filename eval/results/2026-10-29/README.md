# Reference evaluation results, 2026-10-29

Raw reports behind the retrieval numbers in the README and docs/evaluation.md, written by
`uv run python scripts/reference_results.py 2026-10-29`. Generated from the
reports in this directory; `tests/unit/test_reference_results.py` checks it stays so.

- Dataset: `eval/golden_v2.jsonl`, 94 questions (dev 36, test 58), SHA-256 `52d9416d49f8…`
- Corpus: 9 documents, fingerprint `3a8f8c2d3e62…`
- Code: `f1d0d5c`
- Retrieval only, top 5, offline: no generator was called and nothing was paid for.

| Configuration | Report | Test hit rate | Test MRR | Dev hit rate | Dev MRR | Unanswerable abstained (test) | False abstention | Retrieval p50 / p95 ms | Rerank p50 ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| hashing, lexical (BM25 only) | [hashing-lexical.json](hashing-lexical.json) | 0.978 | 0.909 | 0.967 | 0.890 | 0.167 | 0.000 | 1 / 1 | - |
| hashing, vector | [hashing-vector.json](hashing-vector.json) | 0.826 | 0.768 | 0.900 | 0.788 | 0.000 | 0.000 | 1 / 1 | - |
| hashing, hybrid | [hashing-hybrid.json](hashing-hybrid.json) | 0.957 | 0.859 | 0.933 | 0.878 | 0.000 | 0.000 | 1 / 1 | - |
| bge-small, lexical (BM25 only) | [bge-small-lexical.json](bge-small-lexical.json) | 0.978 | 0.909 | 0.967 | 0.890 | 0.167 | 0.000 | 1 / 1 | - |
| bge-small, vector | [bge-small-vector.json](bge-small-vector.json) | 1.000 | 0.906 | 0.967 | 0.925 | 0.333 | 0.000 | 6 / 9 | - |
| bge-small, hybrid | [bge-small-hybrid.json](bge-small-hybrid.json) | 1.000 | 0.931 | 0.967 | 0.933 | 0.333 | 0.000 | 6 / 8 | - |
| bge-small, hybrid, MiniLM-L-6 reranking 5 candidates | [bge-small-hybrid-rerank5.json](bge-small-hybrid-rerank5.json) | 1.000 | 0.946 | 0.967 | 0.967 | 0.333 | 0.000 | 42 / 75 | 33 |
| bge-small, hybrid, MiniLM-L-6 reranking 10 candidates | [bge-small-hybrid-rerank10.json](bge-small-hybrid-rerank10.json) | 1.000 | 0.946 | 0.967 | 0.967 | 0.333 | 0.000 | 110 / 153 | 102 |
| bge-small, hybrid, MiniLM-L-6 reranking 20 candidates | [bge-small-hybrid-rerank20.json](bge-small-hybrid-rerank20.json) | 1.000 | 0.946 | 1.000 | 0.978 | 0.333 | 0.000 | 240 / 281 | 232 |
| bge-small, hybrid, MiniLM-L-12 reranking 20 candidates | [bge-small-hybrid-rerank20-minilm-l12.json](bge-small-hybrid-rerank20-minilm-l12.json) | 1.000 | 0.953 | 1.000 | 0.978 | 0.333 | 0.000 | 462 / 519 | 454 |
| bge-small, hybrid, jina-reranker-v1-tiny reranking 20 candidates | [bge-small-hybrid-rerank20-jina-tiny.json](bge-small-hybrid-rerank20-jina-tiny.json) | 0.978 | 0.957 | 1.000 | 0.961 | 0.333 | 0.000 | 212 / 257 | 204 |

## Notes and limitations

- **hashing, hybrid**: The offline configuration that the CI gate (eval/thresholds.json) checks.
- **bge-small, lexical (BM25 only)**: Lexical mode computes no vector score, so the embeddings play no part: ranking and abstention (only when BM25 finds nothing) match hashing, lexical.
- **bge-small, hybrid, MiniLM-L-6 reranking 5 candidates**: The recommended configuration and the default candidate count.
- **bge-small, hybrid, MiniLM-L-6 reranking 20 candidates**: The default before 2026-10-29.
- **bge-small, hybrid, MiniLM-L-12 reranking 20 candidates**: An alternative reranker, kept for comparison. ADR-0005 compared it on golden v1; this is golden v2. It has no calibrated abstention threshold, so it never abstains on its score.
- **bge-small, hybrid, jina-reranker-v1-tiny reranking 20 candidates**: An alternative reranker, kept for comparison. ADR-0005 compared it on golden v1; this is golden v2. It has no calibrated abstention threshold, so it never abstains on its score.
- The 94 questions were written alongside a 9-document synthetic corpus: these are a
  regression baseline for this project, not a benchmark.
- Latency is from one laptop CPU run, with other work possible on the machine; compare
  latencies only within this table, and expect them to vary between runs.
- Answer quality isn't measured here: the Claude evaluation hasn't run.
