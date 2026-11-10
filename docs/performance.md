# Performance

What is measured, the targets and why, how to run the benchmark, what it found, and how
regressions are caught. Sizing guidance for operators is in
[operations.md](operations.md#sizing).

## What is measured

| Metric | Definition | Where it comes from |
| --- | --- | --- |
| Retrieval latency | Embedding the question, vector and BM25 search, fusion, loading the chunks, and reranking when it is on | The pipeline's stage timer (`timings_ms.retrieval`; `rerank` is reported separately and included) |
| Generation latency | The generator call, including citation mapping | `timings_ms.generation` |
| Total latency | Retrieval, the abstention decision and generation, in process | `timings_ms.total` |
| Time to first token | From the start of a streamed question to its first text | `timings_ms.first_token` |
| End-to-end latency | A `POST /v1/query` as a client sees it, including HTTP, the request guard and serialization | The load test's clients |
| Throughput | Questions per second in one thread, and requests per second at a stated concurrency | The benchmark |
| Ingestion throughput | Documents and chunks per second through one worker, including parsing and embedding | The benchmark |
| CPU | CPU time per question in process; the server's CPU per request where its process metrics exist (Linux) | `time.process_time`, `process_cpu_seconds_total` |
| Memory | Peak resident memory of the benchmark process, which holds the index as one API process does | The operating system |
| Storage | Database size and bytes per chunk | The database file |
| Tokens and cost | Input and output tokens the provider reported, and their cost at list prices | The generator's usage; unavailable offline |

Latency is always reported as p50, p95, p99 and the maximum, with the minimum and mean in the
JSON. Percentiles interpolate linearly between ranks.

## Targets

From [requirements.md](requirements.md#7-success-criteria), at the reference size of 10,000
chunks. They are limits in `eval/bench_thresholds.json`, so CI fails if one is missed.

| Target | Limit | Why this number |
| --- | --- | --- |
| Retrieval p95 | under 300 ms | Retrieval should stay a small part of an answer: a model's first token typically takes over a second, so 300 ms keeps retrieval under a quarter of the wait |
| Time to first token p95 | under 3 s | The point where a reader starts to doubt that something is happening; streaming exists to stay under it |
| A 100-page text PDF | under 60 s | A long report should be searchable within a minute of upload, on one worker |
| Load-test errors | 0 | The service must not fail requests it has capacity for |

The first-token target is only meaningful with the Claude generator, which hasn't run live.
Offline, it measures the pipeline's own overhead before the first token.

## The benchmark

`rag bench` runs on a temporary database and never touches the configured one.

```bash
make bench                                   # what CI runs: 10,000 chunks, offline
rag bench --output reports/bench             # with the configured models
rag bench --baseline before.json --thresholds eval/bench_thresholds.json
rag bench --save-baseline before.json        # record a run to compare with later
```

| Phase | What happens |
| --- | --- |
| Ingestion | The evaluation corpus, then synthetic documents built from its sentences with a fixed seed, until the index holds `--chunks` chunks. One document at a time, as one worker does, with the configured parser isolation |
| PDF | A generated text PDF of `--pdf-pages` pages, ingested end to end |
| Questions | The golden question set, in process, `--repeat` times after `--warmup` unrecorded questions, which load the vector matrix and the models |
| Streams | `--stream-samples` streamed answers |
| HTTP | A real `rag serve` process on the same index, with rate limits off, `--concurrency` clients and `--requests` questions after a warm-up of two requests per client |

The report (`benchmark.json`, with a `benchmark.md` summary) records what is needed to repeat
and compare a run: the environment (operating system, machine, CPU count, Python and SQLite
versions, the code's version and commit), the configuration (embedding model, reranker,
generator, retrieval mode, `top_k`, candidate pool, chunk size and overlap), the workload
(the corpus fingerprint, the generator's version and seed, the dataset's SHA-256 and
categories, every option), every individual measurement, and the gate's result.

For numbers worth comparing, run it on an idle machine. Caches are warm for the measured
phases: the warm-up loads them. Nothing in the offline configuration uses the network.
With `RAG_GENERATOR=anthropic` the benchmark calls a paid API for every question, so it
refuses to run without `--allow-paid` and says how many calls it would make.

## Results

Measured on 2026-11-10 at commit `4e96cd0`, on a laptop (Ryzen 9 5900HX, 16 logical CPUs,
Windows 11, Python 3.14), with 10,002 chunks in 161 documents, 94 questions asked 3 times,
and the extractive generator. Two configurations: the offline default, and the recommended
one with bge-small embeddings and the cross-encoder reranking 5 candidates.

| Latency (ms) | Offline p50 | p95 | p99 | Recommended p50 | p95 | p99 |
| --- | --- | --- | --- | --- | --- | --- |
| Retrieval | 8.4 | 12.8 | 17.1 | 137.0 | 159.9 | 169.7 |
| of which reranking | | | | 112.1 | 129.2 | 139.0 |
| Total, in process | 8.8 | 13.2 | 17.8 | 137.7 | 160.5 | 170.8 |
| First token, streamed | 9.3 | 13.1 | 14.2 | 137.5 | 154.8 | 155.7 |
| HTTP, 4 concurrent clients | 19.2 | 28.3 | 59.0 | 369.4 | 423.8 | 433.9 |

| Throughput and resources | Offline | Recommended |
| --- | --- | --- |
| Ingestion, one worker | 326 chunks/s | 16.3 chunks/s |
| 100-page text PDF (305 chunks) | 0.9 s | 17.7 s |
| Questions, one thread | 115/s | 7.6/s |
| HTTP, 4 concurrent clients | 191 requests/s | 11.2 requests/s |
| CPU per question | 12 ms | 1,683 ms, across cores |
| Peak memory | 191 MiB | 1,562 MiB |
| Database | 53.6 MiB, 5.5 KiB per chunk | 53.7 MiB, 5.5 KiB per chunk |

Every target is met in both configurations: retrieval p95 is 13 ms and 160 ms against
300 ms, and the PDF takes 0.9 s and 17.7 s against 60 s.

### What drives the numbers

- **Reranking dominates retrieval** in the recommended configuration: 112 of 137 ms. It
  scales with the candidates scored (`RAG_RERANK_CANDIDATES`), about 22 ms each here.
  Reranking 20 candidates took about 250 ms and answered one more development question,
  which is why 5 is the default
  ([ADR-0005](decisions/0005-reranking-and-abstention.md)).

  The evaluation's 75 ms for this stage was measured on a 44-chunk corpus with shorter
  chunks; these chunks are about three times as long.
- **Embedding the question** costs most of the rest: the two configurations differ by about
  16 ms outside reranking.
- **Embedding dominates ingestion**: 16 chunks per second with bge-small against 326 with
  the hashing embedder, on the same parser and chunker.
- **The models use every core.** One question takes 138 ms of wall time but 1.7 s of CPU,
  so concurrent questions compete for the same cores: 4 clients get 11.2 requests per
  second in total, against 7.6 for one thread.
- **Index size** affects exact vector search linearly, but it is a small term: about 0.5 ms
  at 10,000 chunks and 6 ms at 100,000 ([operations.md](operations.md#sizing)).
- **`top_k`, chunk size and the model** set the prompt size, and with a hosted model the
  input tokens and cost. Output length and the model set generation time, which is expected
  to be the largest part of an answer by far once Claude is the generator. That is not
  measured yet.

### Load and saturation

The offline configuration with 400 requests at each concurrency:

| Concurrent clients | Requests/s | p50 (ms) | p95 (ms) | p99 (ms) | Errors |
| --- | --- | --- | --- | --- | --- |
| 1 | 88 | 11 | 16 | 21 | 0 |
| 4 | 171 | 23 | 32 | 47 | 0 |
| 16 | 165 | 91 | 137 | 158 | 0 |
| 64 | 158 | 378 | 478 | 635 | 0 |

Throughput peaks at about 170 requests per second by 4 clients and stays there. Beyond
that, added clients only add queueing time, and nothing fails at 64. That is what one
Python process gives: the handlers run in a thread pool, and the parts of a request that
hold the interpreter lock don't run in parallel.

## Capacity planning

Assumptions: one process, the recommended configuration, the hardware above.

- **Questions, without a hosted model:** about 11 per second per machine, CPU-bound in
  the embedding and reranking models. More uvicorn workers don't add much on the same
  machine, because the models already use every core.
- **Questions, with Claude:** each request waits on the model for seconds while using
  little local CPU, so concurrency is bounded by the provider's rate limits and by the
  thread pool (40 threads by default) before the CPU. At 5 s per answer, 40 threads carry
  about 8 answers per second. The account's requests and tokens per minute are the first
  limit to check.
- **Rate limits** default to 60 questions per client per minute; 11 per second is 660 per
  minute, so about ten clients at their limit saturate one machine.
- **Ingestion:** 16 chunks per second is about 58,000 chunks, or roughly 49 MB of text, per
  hour per machine. `RAG_INGEST_WORKERS` above 1 doesn't multiply it: embedding already
  uses every core.
- **Index size:** exact search and the in-memory matrix are comfortable to about 100,000
  chunks per process (150 MB of vectors with bge-small). Beyond that, plan the move to
  PostgreSQL with pgvector ([ADR-0002](decisions/0002-v0-storage-and-scope.md)).
- **Storage:** 5.5 KiB per chunk; [data-lifecycle.md](data-lifecycle.md#growth) covers the
  rest.

## Regression gate

`eval/bench_thresholds.json` holds two kinds of rule, applied by `rag bench --thresholds`:

- **Limits** are the targets above. They hold on any machine, and missing one fails.
- **Regression rules** compare a run with a baseline, per metric, in the direction that is
  worse. Each has a `warn_pct` and a `fail_pct`, and a `min_delta` below which a change is
  noise, so 0.4 ms becoming 0.8 ms isn't reported as doubling.

| Metric | Warns at | Fails at |
| --- | --- | --- |
| Latency (total, retrieval and HTTP, p95 and p99); CPU per question; PDF ingestion time | 100% worse | 300% worse |
| HTTP requests per second; ingestion chunks per second | 50% lower | 75% lower |
| Peak memory | 15% | 40% |
| Bytes per chunk; tokens per question; estimated cost | 10% | 25% |

The time-based rules are loose on purpose. Two CI runs of the same code, on different
shared runners, differed by 90% in latency and 36% in throughput, while memory moved by
0.1% and bytes per chunk by one byte. So time-based rules warn at twice as slow and fail at
four times, which catches an algorithmic blow-up rather than drift, and memory, storage
and tokens are held tightly. A metric that neither run measured, such as tokens offline,
passes. Across three CI runs of equivalent code, p95 for a
question was 8.5, 16.1 and 17.3 ms; the baseline is the last of them, a typical runner
rather than the fastest.

A baseline is only compared with a run of the same workload and configuration; otherwise
the comparison is skipped with a note. If the machine differs (operating system,
architecture, CPU count or Python version), would-be failures are reported as warnings,
because hardware alone moves these numbers.

| Exit code | Meaning |
| --- | --- |
| 0 | The gate passed, or only warned |
| 1 | A limit or a regression rule failed, or a warning with `--fail-on-warn` |
| 2 | Refused: a paid generator without `--allow-paid`, or bad arguments |
| 3 | The benchmark couldn't run: unreadable thresholds or baseline, a server that didn't start |

CI's `performance` job runs `make bench` on every push, against the baseline in
`eval/bench/baseline-ci.json`, which was recorded on CI's own runners. It shows the summary
on the run page and uploads the full report. To refresh the baseline after an intended
change, download the `benchmark` artifact of a run on `main`, remove its `measurements` and
`gate` keys, and commit it as the baseline.

## Cost

With the Claude generator, cost is tokens times the list price. The benchmark records input
and output tokens per question and estimates the cost from the price table in
`evaluation/plan.py`, and the gate has rules for both. The drivers are `top_k` and chunk
size (input), the answer's length and thinking (output), and how many questions abstain
before generation, which cost nothing. `rag eval --plan --count-tokens` gives exact input
tokens before a paid run ([evaluation.md](evaluation.md#paid-evaluation-with-claude)).

## Not measured yet

- Generation latency, time to first token, tokens and cost with Claude: the account has no
  API credit.
- The server's CPU and memory during the load test on Windows; they are measured on Linux,
  so CI's reports have them.
- Soak tests over hours, and ingestion running during a load test.
- More than one machine.
