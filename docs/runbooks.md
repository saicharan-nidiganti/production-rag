# Runbooks

What to do when something goes wrong: how serious it is, how to find the cause, a procedure
for each kind of failure, and how to confirm the service has recovered. The signals referred
to are in [observability.md](observability.md), the commands in
[operations.md](operations.md) and [data-lifecycle.md](data-lifecycle.md).

## Severity

| Level | Meaning | Examples | Respond |
| --- | --- | --- | --- |
| SEV1 | The service is down for everyone, or data is exposed or lost | `/ready` fails; the database is corrupted; the API key or the Anthropic key leaked | At once, until it is mitigated |
| SEV2 | A main function fails or is badly degraded | Every answer fails (502 or 503); ingestion has stopped; p95 is several times its normal value | Within the hour |
| SEV3 | A partial or minor fault with a workaround | Some documents fail to ingest; more "not enough evidence" answers than usual; one client is rate-limited | The same working day |
| SEV4 | No user impact yet | A warning in the benchmark; a dependency advisory without a fix; disk filling slowly | Planned work |

Mitigate first, then find the cause. Restoring service comes before understanding why.

## First five minutes

The same steps for any report, before choosing a runbook:

1. **Is it up?** `curl -s localhost:8000/health`, then `curl -s localhost:8000/ready`. A
   503 from `/ready` says why in `checks`.
2. **What is failing?** In the metrics: `rag_http_requests_total` by `status` and `route`,
   `rag_generation_failures_total`, `rag_documents` by `status`,
   `rag_ingestion_oldest_queued_seconds`. Without Prometheus, filter the logs for
   `level` `ERROR` and `WARNING`.
3. **Since when?** Compare with the last deployment, configuration change and upload batch.
   `startup.complete` in the logs records the version and configuration of each start.
4. **One request.** Take a failing request's `X-Request-ID`, or the `request_id` in an error
   body, and read every log line with it
   ([observability.md](observability.md#reconstructing-a-failed-request)).
5. **Choose the runbook** below from what you found. If a change went out just before, roll
   it back first ([Rollback](#rollback)).

## The service is down or not ready

Signals: `/health` doesn't answer, or `/ready` is 503; `rag_database_up` or
`rag_schema_at_head` is 0.

| `/ready` says | Cause | Do |
| --- | --- | --- |
| No answer at all | The process isn't running | Read the last log lines. A configuration error prints one line per bad variable and exits; fix the variable and start again |
| `schema: not at head` | A new version is running on an old schema | `rag db upgrade`, or start with `rag serve --migrate`; the image does this itself |
| `database: unavailable` | The file is missing, unreadable or locked | Check that `/data` is mounted and writable by uid 10001. Then `rag db verify`; if `sqlite_integrity` fails, go to [The database is damaged](#the-database-is-damaged) |
| 200, but requests fail | Not a readiness problem | Go back to the first five minutes, step 2 |

## Answers fail: the model provider

Signals: HTTP 502 `generation_failed` or 503 `generation_unavailable`;
`rag_generation_failures_total` rising; `generation.failed` in the logs.

| `retryable` | Message | Cause | Do |
| --- | --- | --- | --- |
| `true` | rate limit reached | The account's requests or tokens per minute | Wait; lower `RAG_RATE_LIMIT_QUERY_PER_MINUTE` so clients are slowed before the provider is; ask Anthropic for a higher limit |
| `true` | overloaded, HTTP 5xx, could not reach | The provider or the network | Check Anthropic's status page and outbound HTTPS. The SDK already retries; clients should retry later |
| `false` | credentials were rejected, or no credentials configured | `ANTHROPIC_API_KEY` is wrong, revoked or unset | Set a valid key and restart |
| `false` | no credit left | The account's balance is empty | Add credit in the Claude Console |
| `false` | rejected the request (HTTP 400 or 404) | The model name or a request option | Check `RAG_ANTHROPIC_MODEL` and the changelog; the full error is in the log line |

To keep answering during a long outage, set `RAG_GENERATOR=extractive` and restart: answers
become quoted passages with citations, with no model call. Quote `anthropic_request_id` from
`generation.completed` when reporting a problem to Anthropic.

## Answers are wrong, unsupported or missing

Signals: users report wrong or empty answers; the share of `insufficient_evidence` in
`rag_queries_total` changes; `citations` is 0 in `query.completed`.

1. Ask the same question through `POST /v1/search`. It shows the passages and their scores,
   with no model involved.
2. Decide which part failed:

| What search shows | Cause | Do |
| --- | --- | --- |
| The right passage isn't there | The document isn't `ready`, or was never uploaded | `GET /v1/documents?status=failed`; fix and retry, or upload it |
| The right passage ranks low | Lexical-only embeddings (`startup.lexical_only_embeddings`), stale chunks (`rag_stale_chunks` above 0), or reranking is off | `RAG_EMBEDDING_PROVIDER=fastembed`; `rag reindex`; `RAG_RERANKER=fastembed` |
| An old version of a document is found | It was uploaded as a new document, not a replacement | Delete the old one, or upload with `?replaces={id}` next time |
| The right passage is there, but the answer says there isn't enough evidence | The abstention threshold is too strict for this corpus, or the model declined | Compare `best_vector_score` and `best_rerank_score` in `query.completed` with the thresholds ([evaluation.md](evaluation.md)); recalibrate with `rag eval` on your own questions |
| The right passage is there, but the answer is wrong or cites nothing | The model | An answer that cites nothing is never reported as `answered`. Check `stop_reason` in `generation.completed`: `max_tokens` means raise `RAG_ANTHROPIC_MAX_TOKENS` or lower `RAG_TOP_K` |
| A passage tells the model what to do | A prompt-injection attempt in a document | Treat as a [security incident](#security-incidents); delete the document |

After a change, run `rag eval --thresholds eval/thresholds.json` to confirm retrieval
quality hasn't regressed.

## The service is slow

Signals: p95 of `rag_query_stage_seconds{stage="total"}` or of
`rag_http_request_duration_seconds` rises.

| Slow stage | Cause | Do |
| --- | --- | --- |
| `generation`, `first_token` | The provider, the model, or long answers | Nothing local is slow. Check the provider; use streaming so text shows early |
| `rerank` | Many candidates, or a busy CPU | Lower `RAG_RERANK_CANDIDATES`; each costs about 22 ms on a laptop CPU ([performance.md](performance.md#what-drives-the-numbers)) |
| `retrieval` without reranking | A large index, or the question's embedding | Check `rag_chunks` against the sizing in [operations.md](operations.md#sizing) |
| HTTP time much higher than `total` | Requests queue: more concurrent clients than one process serves | See [performance.md](performance.md#load-and-saturation); add a machine or reduce load |
| Everything, during uploads | Ingestion embeds on the same cores | Upload outside peak hours, or set `RAG_INGEST_WORKERS=0` on the serving process and run ingestion in another |

`rag bench` on the same machine tells you whether the build or the machine is slow: compare
it with an earlier report.

## Ingestion is stuck or failing

Signals: documents stay `queued` or `processing`; `rag_ingestion_oldest_queued_seconds`
grows; `rag_documents{status="failed"}` grows.

| Symptom | Cause | Do |
| --- | --- | --- |
| Everything stays `queued`; `rag_ingestion_workers` is 0 | No process runs workers | Start one with `RAG_INGEST_WORKERS` of 1 or more. A process that started with the schema behind runs none: upgrade, then restart |
| Workers run, but nothing completes; `ingest.worker_error` | The database is unavailable to them | See [The service is down or not ready](#the-service-is-down-or-not-ready) |
| One document stays `processing` | Its worker died | Nothing: another worker takes it over when its lease lapses (`RAG_INGEST_LEASE_S`), logged as `ingest.recovered`. After `RAG_INGEST_MAX_ATTEMPTS` interruptions it fails with `interrupted` |
| `failed` with `extraction_failed`, `unsupported_document_type` | The file: no text layer, encrypted, macro-enabled, or not what its extension says | The message says which. Fix the file and upload it again |
| `failed` with `parse_timeout` or `parse_failed` | A very large, damaged or hostile file: the parser ran out of time or memory, or crashed | Raise `RAG_PARSE_TIMEOUT_S` or `RAG_PARSE_MEMORY_MB` if the file is legitimate |
| `failed` with `internal_error` | A bug | The stack trace is in `ingest.failed`. After a fix: `POST /v1/documents/{id}/retry` |

Failed documents keep their upload until retried, deleted or pruned
(`rag db prune --failed-older-than 30`).

## The database is damaged

Signals: `rag db verify` fails; SQLite reports "database disk image is malformed"; searches
miss text that is in a document.

1. Stop the service, so nothing writes while you look.
2. `rag db verify`. The failing check decides:

| Failing check | Do |
| --- | --- |
| `search_index` only | `rag db rebuild-search-index`, then verify again. Nothing is lost |
| `embeddings`, or stale chunks | `rag reindex` |
| `sqlite_integrity`, `foreign_keys`, `ready_documents`, `partial_chunks`, `uploads`, `index_state` | Restore: `rag db verify --file <backup>` on the newest backup, then `rag db restore <backup> --yes`. The damaged file is kept as `rag.pre-restore-<time>.db` |

Then start the service, check `/ready`, and upload again any documents ingested after the
backup was made. If the disk is full, free space before anything else: old backups
(`rag db backup --keep N` prunes them), pre-restore copies and failed documents.

## Security incidents

| Event | Signals | Do |
| --- | --- | --- |
| The API key is exposed | Found in a log, a repository or a chat; unknown clients in `http.request` | Generate a new key, set `RAG_API_KEY`, restart. There is one key, so every client must change at once. Review `http.request` since the exposure for uploads and deletions |
| The Anthropic key is exposed | The same; or unexpected usage in the Claude Console | Revoke it in the Claude Console, create another, set `ANTHROPIC_API_KEY`, restart. Check the Console's usage for calls you didn't make |
| Key guessing or abuse | `security.auth_failed` and `security.rate_limited` from one address | The built-in limits already slow it. Block the address at the proxy |
| A malicious or injecting document | An answer follows instructions from a document; an odd file was uploaded | Delete the document. Find who uploaded it from the `request_id` of its `ingest.queued` line. Uploads should be limited to trusted users |
| A vulnerability in a dependency or the image | pip-audit, CodeQL, Dependabot or Trivy reports it | Upgrade, run `make check` and the evaluation gate, release. If no fix exists, record the exposure and mitigation in [security.md](security.md) |
| Data that must be erased | A request to remove content | Delete the documents, then follow [data-lifecycle.md](data-lifecycle.md#deletion): `VACUUM`, and expire the backups that hold it |

Keep the logs from the period: they hold who did what, by request id, and never hold
content or keys.

## Rollback

| What changed | Roll back by |
| --- | --- |
| Configuration | Restore the previous environment and restart. `startup.complete` shows what each start ran with |
| The code, with no migration | Run the previous version: the previous image tag, or the previous wheel |
| The code, with a migration | Stop, `rag db restore <the backup taken before the upgrade> --yes`, then run the previous version. There is no downgrade command |
| An embedding model | Set the previous model and run `rag reindex`; lexical search keeps working meanwhile |
| A document | Upload the previous version with `?replaces={id}` |

Take the backup before every upgrade ([operations.md](operations.md#upgrading-and-rolling-back)).
Without one, a release that migrated can only be rolled forward.

## Verifying recovery

An incident is over when all of these hold, not when the alert stops:

1. `/ready` returns 200 with `schema_revision` equal to `expected_revision`.
2. `rag db verify` passes.
3. A question with a known answer returns `answered` with a citation, and
   `POST /v1/query/stream` ends with a `done` event.
4. An upload reaches `ready`.
5. The metrics are back to normal for 15 minutes: no new `rag_generation_failures_total`,
   5xx under 1%, `rag_ingestion_oldest_queued_seconds` at 0.
6. `scripts/smoke_test.py` passes against a scratch copy if the code or the image changed.

## Incident record

Write it while it happens; five lines beat a perfect report later.

```text
Incident:   short title
Severity:   SEV1-4
Detected:   time (UTC), and by what: alert, user report, log
Impact:     who couldn't do what, for how long
Timeline:   time - what was seen or done
Cause:      what failed, and why it could
Mitigation: what restored service
Evidence:   request ids, log lines, metric graphs, the commit or configuration change
Follow-up:  actions with an owner and a date
```

## After the incident

For SEV1 and SEV2, within a week, review without blame:

1. What happened, from the record's timeline.
2. How it was detected, and whether an alert should have caught it sooner.
3. Why it could happen: the cause behind the cause.
4. What made recovery slow.
5. Actions: a test that would have caught it, an alert, a runbook entry here, a fix. Each
   becomes an issue.

A runbook that was wrong or missing during the incident is itself an action.
