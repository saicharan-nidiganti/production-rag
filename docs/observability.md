# Observability

What the backend records about its own behaviour, how to connect a failed request to its
cause, and what it never records. This page covers the requirements and what v0 implements;
[operations.md](operations.md) covers deploying and alerting on it.

## Principles

- **Correlate everything by an identifier.** Every log line written during a request
  carries its `request_id`. Background ingestion, which outlives the upload request, is
  correlated by `document_id`.
- **Record behaviour, never content.** Logs and metrics describe what happened: counts,
  scores, timings, statuses, error codes. They never contain question text, document text,
  retrieved passages, answers or credentials ([Sensitive data](#sensitive-data)).
- **Stay vendor-neutral.** Logs are JSON lines on stderr and metrics are in the Prometheus
  text format. Every common log and metrics stack reads both, so no platform is chosen here.
- **Keep label sets bounded.** Metric labels take values from small fixed sets, such as
  route templates, status codes, models and stages. A metric never gets a series per
  document, client or URL.

## Signals

| Signal | Where | Use it for | Status |
| --- | --- | --- | --- |
| Logs | JSON lines on stderr, one event per line | Reconstructing one request or document; auditing security refusals | Implemented |
| Metrics | `GET /metrics`, Prometheus text format | Rates, latencies, error ratios, queue depth, alerting | Implemented |
| Health | `GET /health` (liveness), `GET /ready` (readiness, with index and queue counts) | Orchestrator probes, deploy checks | Implemented |
| Evaluation reports | `rag eval` JSON and Markdown reports, gated by `eval/thresholds*.json` | Retrieval and answer quality; not a runtime signal | Implemented ([evaluation.md](evaluation.md)) |
| Traces | Not emitted | See [Tracing](#tracing) | Deferred |

## Request correlation

1. A request may send `X-Request-ID`. A value of 1 to 64 letters, digits, `.`, `_` or `-`
   is used as it is; anything else is replaced with a new random id, so a client can't
   inject text into the logs.
2. The id is set for the request's context, and every log line written while handling it,
   in any module, carries it as `request_id`.
3. The response returns it as `X-Request-ID`, and error bodies include it as
   `error.request_id`, so a client can quote it in a support request.
4. A call to Claude also logs `anthropic_request_id`, which Anthropic support can look up.
5. An upload logs `ingest.queued` with both the `request_id` and the new `document_id`.
   The worker's later events (`ingest.started`, `ingest.completed`, `ingest.failed`) carry
   the `document_id`, which connects them to the upload.

## Logs

Each line is one JSON object with `ts` (UTC, ISO 8601), `level`, `logger`, `event` and,
within a request, `request_id`, followed by the event's own fields. `RAG_LOG_LEVEL` sets the
level. Configured secrets are replaced by `[REDACTED]` anywhere in a line, exception text
included, as a backstop: no code logs them.

### Events

| Event | Level | When | Fields |
| --- | --- | --- | --- |
| `startup.complete` | INFO | The app is ready to serve | `version`, `generator`, `embedding_model`, `auth_enabled`, `ingest_workers`, `rate_limits_per_minute`: the effective configuration, without secrets |
| `startup.lexical_only_embeddings` | WARNING | Started with the hashing embedder | |
| `startup.shared_keyword_statistics` | WARNING | Started with accounts on SQLite, where keyword ranking uses every user's documents' word statistics ([security.md](security.md#known-gaps)) | `hint` |
| `startup.schema_not_at_head` | ERROR | The schema is behind at startup; workers don't start | |
| `startup.vector_index_missing` | WARNING | Approximate vector search is on, but the model's index is missing or invalid; searches are exact until `rag db create-vector-index` has run | `search`, `pgvector`, `index`, `hint` |
| `startup.vector_search_unavailable` | ERROR | Approximate vector search is on, but pgvector is older than 0.8: every search would fail, and `/ready` reports 503 | `search`, `error` |
| `startup.vector_index_unknown` | WARNING | The index couldn't be checked at startup, usually because the database wasn't reachable | `error` |
| `vector_index.created`, `vector_index.dropped` | INFO | `rag db create-vector-index` or `drop-vector-index` changed an index | `model`, and for a build `index`, `seconds`, `size_bytes` |
| `http.request` | INFO | Every response | `method`, `path`, `status`, `duration_ms` (until the response started) |
| `request.unhandled_error` | ERROR | An unexpected exception; the client gets 500 `internal_error` | `exc_info` |
| `query.completed` | INFO | A question was answered, streamed or not | `status`, `mode`, `retrieved`, `citations`, `best_vector_score`, `best_rerank_score`, `abstained_before_generation`, `model`, `streamed`, `timings_ms` (`retrieval`, `rerank`, `generation`, `first_token`, `total`) |
| `generation.completed` | INFO | The model returned a message | `model`, `stop_reason`, `anthropic_request_id`, `streamed`, `input_tokens`, `output_tokens` |
| `generation.refused` | WARNING | The model declined to answer | `category` |
| `generation.failed` | ERROR | The generator raised; the client gets 502, 503 or an `error` event | `retryable`, `streamed`, `exc_info` |
| `ingest.queued` | INFO | An upload was accepted | `document_id`, `content_type`, `size_bytes` |
| `ingest.duplicate` | INFO | An upload matched a stored document's content | `document_id`, `status` |
| `ingest.requeued` | INFO | A retry, or an identical re-upload of a failed document | `document_id`, `reason` (`retry` or `duplicate_upload`) |
| `ingest.workers_started` | INFO | Worker threads started | `threads` |
| `ingest.started` | INFO | A worker claimed a document | `document_id`, `attempt` |
| `ingest.completed` | INFO | A document became `ready` | `document_id`, `attempt`, `content_type`, `size_bytes`, `chunks`, `embedding_model`, `duration_ms` |
| `ingest.failed` | WARNING for expected failures (bad file, no text), ERROR otherwise | A document became `failed` | `document_id`, `attempt`, `error_code`, and `exc_info` when unexpected |
| `ingest.recovered` | WARNING | A worker took over a document whose lease had lapsed | `document_id`, `attempt` |
| `ingest.released` | INFO | Shutdown handed a document back to the queue | `document_id`, `attempt` |
| `ingest.claim_lost` | WARNING | Another worker took the document over; this one's work is discarded | `document_id`, `attempt` |
| `ingest.lease_renewal_failed` | WARNING | A lease couldn't be renewed | `document_id` |
| `ingest.worker_error` | ERROR | A worker loop failed; it backs off and continues | `retry_in_s`, `exc_info` |
| `reindex.completed` | INFO | `rag reindex` finished | `chunks` (re-embedded) |
| `security.auth_failed` | WARNING | A missing or wrong API key | `reason` (`missing` or `invalid`), `client`, `method`, `path` |
| `security.rate_limited` | WARNING | A request over a rate limit | `operation`, `retry_after_s`, `client`, `method`, `path` |
| `security.login_failed` | WARNING | A sign-in with a wrong password, an unknown address or a disabled account | `client`; never the address or the password |
| `security.csrf_failed` | WARNING | A session request that changes state without the right `X-CSRF-Token` | `client`, `method`, `path` |
| `account.created`, `account.signed_in`, `account.password_changed`, `account.enabled`, `account.disabled`, `account.role_changed`, `account.deleted`, `account.token_created`, `account.token_revoked` | INFO | An account changed | `user_id`, and `role` or `token_id` where it applies; never an email address, password or token |
| `security.request_too_large` | WARNING | A body over its size limit | `limit_bytes`, `declared_bytes` or `received_bytes`, `client`, `method`, `path` |
| `metrics.database_unavailable` | WARNING | A scrape couldn't read the database | `error` (the exception type) |

Evaluation runs report to the terminal and to their report files rather than to the logs.

## Metrics

`GET /metrics` serves the Prometheus text format. It sits behind the API key when one is set,
and draws from the `read` rate limit. `RAG_METRICS_ENABLED=false` turns it off. Durations are
in seconds and sizes in bytes. Counters appear with a `_total` suffix, and histograms as
`_bucket`, `_sum` and `_count`.

### Requests, questions and generation

Counted in each process as events happen:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `rag_build_info` | gauge | `version` | Always 1; the running version |
| `rag_http_requests_total` | counter | `method`, `route`, `status` | Responses, by route template (`/v1/documents/{document_id}`), never the raw path; `unmatched` for unknown paths |
| `rag_http_request_duration_seconds` | histogram | `method`, `route` | Time until the response started; for a stream, until its headers were sent |
| `rag_queries_total` | counter | `status`, `streamed` | Questions answered, by `answered`, `insufficient_evidence` or `refused` |
| `rag_query_abstentions_before_generation_total` | counter | | Questions declined on retrieval evidence alone, without a model call |
| `rag_query_stage_seconds` | histogram | `stage` | `retrieval` (including `rerank`), `rerank`, `generation`, `first_token` (streams) and `total` |
| `rag_generation_tokens_total` | counter | `model`, `kind` | Input and output tokens the provider reported |
| `rag_generation_failures_total` | counter | `retryable` | Failed generations; `true` means a rate limit, overload or outage |
| `rag_generation_refusals_total` | counter | `category` | Answers the model declined |
| `rag_security_refusals_total` | counter | `reason` | `auth_failed`, `rate_limited`, `request_too_large` |
| `rag_rate_limited_total` | counter | `operation` | Rate-limited requests by bucket: `upload`, `query`, `search`, `read`, `auth_failure` |
| `rag_vector_exact_fallbacks_total` | counter | | Approximate vector searches redone exactly because they found fewer results than asked for. Expected for owners with fewer chunks than `RAG_CANDIDATE_POOL`; for larger owners, a sign that the index scan gave up early (see [operations.md](operations.md#approximate-vector-search)) |

### Ingestion

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `rag_ingestion_documents_total` | counter | `outcome` | Documents a worker finished: `ready`, `failed`, `released`, `lost` |
| `rag_ingestion_duration_seconds` | histogram | | Claim to `ready` or `failed` |
| `rag_ingested_chunks_total` | counter | | Chunks written |

### State, read at each scrape

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `rag_database_up` | gauge | | 1 if the database answered the scrape |
| `rag_schema_at_head` | gauge | | 1 if the schema is at the latest migration |
| `rag_documents` | gauge | `status` | Documents by `queued`, `processing`, `ready`, `failed` |
| `rag_chunks` | gauge | | Chunks in the index |
| `rag_stale_chunks` | gauge | | Chunks embedded by a model other than the configured one |
| `rag_ingestion_oldest_queued_seconds` | gauge | | How long the longest-waiting queued document has waited |
| `rag_ingestion_workers` | gauge | | Live worker threads in this process |

The client library adds the standard `process_*` metrics (CPU, memory, open files; Linux
only), `python_gc_*` and `python_info`.

Counters live in each process's memory, like the rate limits. With `uvicorn --workers N`, a
scrape reaches one of the N processes, so either run one process per scrape target or sum
the targets in your queries. The state gauges come from the shared database and are the
same from every process. A restart resets the counters; `rate()` and `increase()` handle
that.

### Scraping and alert rules

```yaml
# prometheus.yml
scrape_configs:
  - job_name: production-rag
    scrape_interval: 15s
    metrics_path: /metrics
    authorization:
      credentials_file: /etc/prometheus/rag-api-key   # RAG_API_KEY, when one is set
    static_configs:
      - targets: ["rag.internal:8000"]
```

The alerts in [operations.md](operations.md#alerts-worth-setting), as rules:

```yaml
groups:
  - name: production-rag
    rules:
      - alert: RagDatabaseDown
        expr: rag_database_up == 0 or rag_schema_at_head == 0
        for: 2m
      - alert: RagIngestionBacklog
        expr: rag_ingestion_oldest_queued_seconds > 900
        for: 5m
      - alert: RagNoIngestionWorkers
        expr: sum(rag_ingestion_workers) == 0 and sum(rag_documents{status="queued"}) > 0
        for: 5m
      - alert: RagServerErrors
        expr: >
          sum(rate(rag_http_requests_total{status=~"5.."}[5m]))
          / sum(rate(rag_http_requests_total[5m])) > 0.01
        for: 5m
      - alert: RagModelUnavailable
        expr: sum(increase(rag_generation_failures_total{retryable="true"}[10m])) > 5
      - alert: RagSlowAnswers
        expr: >
          histogram_quantile(0.95, sum by (le) (rate(rag_query_stage_seconds_bucket{stage="total"}[10m])))
          > 10
        for: 10m
      - alert: RagAbuse
        expr: sum(rate(rag_security_refusals_total[5m])) > 1
        for: 10m
```

Useful dashboard queries:

| Panel | Query |
| --- | --- |
| Requests per second by route | `sum by (route) (rate(rag_http_requests_total[5m]))` |
| p95 answer time | `histogram_quantile(0.95, sum by (le) (rate(rag_query_stage_seconds_bucket{stage="total"}[5m])))` |
| p95 time to first token | the same, with `stage="first_token"` |
| Abstention rate | `sum(rate(rag_queries_total{status="insufficient_evidence"}[1h])) / sum(rate(rag_queries_total[1h]))` |
| Tokens per hour by model | `sum by (model, kind) (increase(rag_generation_tokens_total[1h]))` |
| Ingestion throughput | `sum(rate(rag_ingested_chunks_total[5m]))` |

## Tracing

Not implemented in v0, by decision. A request runs in one process, makes at most one
outbound call (to Claude) and already logs its stage timings in `query.completed`, which is
what a trace of it would show. Distributed tracing becomes worth its cost when a request
crosses processes: workers on other machines, or a separate retrieval service
([roadmap](roadmap.md), Phase 5).

When it is added, with OpenTelemetry, the spans are:

| Span | Parent | Attributes (no content) |
| --- | --- | --- |
| `http.request` | none | method, route, status, `request_id` |
| `rag.query` | `http.request` | mode, status, `streamed`, `abstained_before_generation` |
| `rag.retrieve` | `rag.query` | `candidate_pool`, results, best vector score |
| `rag.rerank` | `rag.retrieve` | candidates, best rerank score |
| `rag.context` | `rag.query` | chunks sent |
| `rag.generate` | `rag.query` | model, stop reason, input and output tokens, `anthropic_request_id` |
| `rag.citations` | `rag.generate` | citations mapped, citations dropped |
| `ingest.document` | linked to the upload's `http.request` | `document_id`, attempt, content type, chunks, outcome |
| `ingest.parse` | `ingest.document` | parser exit status, duration |
| `ingest.embed` | `ingest.document` | batches, model |

## Diagnosing RAG problems

| Symptom | Look at | What it tells you |
| --- | --- | --- |
| Many "not enough evidence" answers | `rag_query_abstentions_before_generation_total` against `rag_queries_total`; `best_vector_score` and `best_rerank_score` in `query.completed` | If scores sit just under the abstention threshold, the corpus lacks the answer or the threshold is too strict ([evaluation.md](evaluation.md)) |
| Irrelevant passages retrieved | `POST /v1/search` with the same question shows the ranked chunks with scores; `mode` in `query.completed` | Hashing embeddings (`startup.lexical_only_embeddings`) or stale chunks (`rag_stale_chunks` above 0, fixed by `rag reindex`) |
| Missing evidence: a document's content never shows up | `GET /v1/documents/{id}` status; `ingest.failed` with its `error_code`; `rag_documents{status="failed"}` | Failed or still-queued documents aren't searchable |
| Answer without support | `citations` = 0 with `status = insufficient_evidence` in `query.completed` | An answer citing nothing is never reported as `answered` |
| Citation failures | `citations` against `retrieved` in `query.completed` | Low citation counts with high retrieval scores point to the model; check `stop_reason` in `generation.completed` |
| Context overflow | `input_tokens` in `generation.completed`; `stop_reason = max_tokens` | Lower `RAG_TOP_K` or `RAG_CHUNK_SIZE`, or raise `RAG_ANTHROPIC_MAX_TOKENS` |
| Slow retrieval | `rag_query_stage_seconds{stage="retrieval"}` and `{stage="rerank"}` | Reranking many candidates, or a corpus past the exact-search sizing ([operations.md](operations.md#sizing)) |
| Slow generation | `stage="generation"` and `stage="first_token"` | The model, the output length, or provider latency |
| Provider or model failures | `rag_generation_failures_total` by `retryable`; `generation.failed` with `exc_info` and `anthropic_request_id` | `true`: rate limit, overload or outage, retry later. `false`: credentials, credit or configuration |

## Sensitive data

| Data | Handling | How |
| --- | --- | --- |
| API keys, the Anthropic key | Excluded | Held as `SecretStr`, never logged; the log formatter also redacts their values anywhere in a line |
| `Authorization` and `X-API-Key` headers | Excluded | Never logged; `security.auth_failed` records only `missing` or `invalid` |
| Question text | Excluded | Never logged or used as a label; `query.completed` records counts, scores and timings |
| Document contents and retrieved passages | Excluded | Never logged; chunk text leaves the process only to the configured generator |
| Generated answers | Excluded | Returned to the client only; not stored or logged |
| Document identity | Logged as an identifier | `document_id` (random); the content hash identifies duplicates and is stored, not logged |
| File names | Excluded from logs | Stored as metadata; they can carry personal information |
| Client addresses | Logged on security events only | `client` on `security.*` events, for abuse investigation; never a metric label |
| Exceptions | Logged, redacted | `exc_info` passes through the same redaction; SDK messages that can echo request details are logged, not returned to clients |

Nothing is sampled: one `http.request` line per request is small enough to keep in full.
Where personal information in questions matters, the absence of question text from logs is
the control; v0 has no per-user data to pseudonymize, since it has no user accounts.

## Reconstructing a failed request

From the `request_id` a client quotes, or one taken from `http.request` with a 5xx status:

1. Filter the logs on the `request_id`: `http.request` gives the route, status and
   duration; the error body gave the client the same id and an error `code`.
2. `request.unhandled_error`, `generation.failed` or `security.*` give the cause, with a
   stack trace for unexpected errors.
3. `query.completed` gives what retrieval found (counts, best scores, mode) and where the
   time went (`timings_ms`); `generation.completed` gives the model, stop reason, tokens and
   `anthropic_request_id`.
4. To see what the model was given, rerun the question through `POST /v1/search`, or
   `rag search`, against the same index: retrieval is deterministic for a given index and
   configuration, and `startup.complete` records that configuration.

For an ingestion failure, filter on the `document_id` instead: `ingest.queued`, then
`ingest.started` and `ingest.failed` with its `error_code`, which `GET /v1/documents/{id}`
also returns.
