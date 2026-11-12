# Operations

Running the backend outside a development laptop: how to deploy, upgrade, size, monitor
and back it up, and what to do when something goes wrong.

## Reference deployment

```mermaid
flowchart LR
    clients(["Clients"]) -- "HTTPS" --> proxy["Reverse proxy<br/>TLS, body limit, rate limits,<br/>no buffering for SSE"]
    proxy -- "HTTP :8000" --> app["production-rag<br/>container or rag serve"]
    app --- data[("Volume /data<br/>rag.db, SQLite in WAL mode<br/>downloaded models")]
    app -- "HTTPS, only with Claude" --> claude{{"Claude API"}}
    probes(["Orchestrator"]) -. "GET /health, /ready" .-> app
    app -. "JSON logs on stderr" .-> logs(["Log pipeline"])
    classDef external fill:#fff4e5,stroke:#c05621,color:#1a202c
    classDef store fill:#e6f0ff,stroke:#2b6cb0,color:#1a202c
    class claude external
    class data store
```

- **One application instance per SQLite database.** SQLite allows a single writer, so the app runs
  on one machine: one container, or one `rag serve` or `uvicorn` process group.
- **A reverse proxy** terminates TLS, caps request bodies, rate-limits across processes and
  must pass `POST /v1/query/stream` through unbuffered ([Streaming](#streaming)).
- **The only outbound call** goes to the Claude API, and only with `RAG_GENERATOR=anthropic`.
  The default configuration makes no network calls at all.
- **State lives in one volume:** the database and, with fastembed, the downloaded models.

## Install and configure

```bash
uv sync --frozen --all-extras --no-dev   # exact versions from uv.lock
cp .env.example .env                     # or set the variables in your process manager
```

Recommended production settings: `RAG_EMBEDDING_PROVIDER=fastembed`,
`RAG_GENERATOR=anthropic`, `ANTHROPIC_API_KEY` from your secret store, `RAG_API_KEY` set, and
`RAG_DATABASE_URL` pointing at a persistent volume.

Pre-download the models into the image or volume so the service doesn't fetch them at
startup. `RAG_FASTEMBED_CACHE_DIR` sets where they live:

```bash
uv run python -c \
  "from production_rag.embeddings import FastEmbedEmbedder; FastEmbedEmbedder('BAAI/bge-small-en-v1.5', '/opt/models')"
# With RAG_RERANKER=fastembed, also:
uv run python -c \
  "from production_rag.retrieval.reranking import FastEmbedReranker; FastEmbedReranker('Xenova/ms-marco-MiniLM-L-6-v2', '/opt/models')"
```

### Container image

Each release is published as `ghcr.io/saicharan-nidiganti/production-rag` (tags `X.Y.Z`, `X.Y` and `latest`), built from
`uv.lock` and tested by [container.yml](../.github/workflows/container.yml) before it is
pushed: it must become ready, ingest a document, answer with citations and stream an answer.

```bash
docker run -d --name rag -p 8000:8000 -v rag-data:/data \
  -e RAG_API_KEY -e ANTHROPIC_API_KEY \
  -e RAG_EMBEDDING_PROVIDER=fastembed -e RAG_RERANKER=fastembed -e RAG_GENERATOR=anthropic \
  ghcr.io/saicharan-nidiganti/production-rag:1.0.0
```

- It runs `rag serve --migrate`: one process on port 8000, as an unprivileged user
  (uid 10001), with a `HEALTHCHECK` on `/health`.
- `/data` holds the SQLite database (`RAG_DATABASE_URL=sqlite:////data/rag.db`) and the
  downloaded models (`RAG_FASTEMBED_CACHE_DIR=/data/models`): mount a volume there.
- With no settings it runs offline: hashing embeddings and extractive answers. `-e NAME`
  without a value passes the variable from your shell, so secrets stay off the command line.
- Run one container per database: SQLite allows a single writer.

## Deploy sequence

1. `uv run rag db upgrade`: apply migrations. The service doesn't migrate on its own unless
   it is started with `rag serve --migrate`, as the container image is. `/ready` returns 503
   until the schema is at head, and ingestion
   workers retry with a backoff (logging `ingest.worker_error`) until then. Revision 0002
   adds ingestion status; documents ingested before it become `ready`.
2. Start the API: `uv run uvicorn --factory production_rag.api.app:app_factory --host 0.0.0.0
   --port 8000 --workers 4`, or `uv run rag serve --host 0.0.0.0` for a single process.
3. Point the liveness probe at `GET /health` and the readiness probe at `GET /ready`.

Run several workers on one machine if you need to. Each keeps its own vector cache, and caches
stay consistent through the `index_state` generation counter. SQLite allows a single writer, so
don't share the database file across machines or over network filesystems
([ADR-0002](decisions/0002-v0-storage-and-scope.md) lists when to move to PostgreSQL).

Put a reverse proxy in front for TLS and to cap request bodies at `RAG_MAX_UPLOAD_BYTES`. The
app enforces the limit too, but only after the body has been received.

## Upgrading and rolling back

1. Read the release notes and the [changelog](../CHANGELOG.md) for new settings and
   behaviour changes.
2. Back up the database: `rag db backup` ([Backups](#backups)).
3. Install the new version: `git pull && uv sync --frozen --all-extras --no-dev`, or pull the
   new image tag.
4. Apply migrations with `rag db upgrade`; the container image does this when it starts.
   `rag db current` prints the schema revision and the latest one.
5. Restart, then check that `/ready` returns 200 with `schema_revision` equal to
   `expected_revision`, and ask one question.

To roll back, run the previous version. If the upgrade applied a migration, restore the
backup from step 2 first with `rag db restore`: there is no `rag db downgrade` command.
0.1.0 and 0.2.0 share schema revision 0003, so moving between them needs no migration.
1.0.0 adds revisions 0004 and 0005, so returning from 1.0.0 to 0.2.0 means restoring the backup.

## Sizing

| Resource | What drives it | Measured or estimated |
| --- | --- | --- |
| Memory | The in-memory vector matrix in each process: chunks × dimensions × 4 bytes (384 for bge-small, 512 for hashing), plus the ONNX models with fastembed | 100,000 bge-small chunks need about 150 MB per process |
| Disk | Extracted text, chunks with overlap, embeddings and the FTS5 index; failed uploads until they are retried or deleted | Measured: 2,743 chunks of about 840 characters with bge-small took 14.8 MB, about 5.3 KiB per chunk, 7.5 times the extracted text |
| CPU, ingestion | Embedding, which uses every core | Measured: about 20 chunks per second with bge-small on an 8-core laptop CPU |
| CPU, queries | Exact vector search, BM25 and the optional reranker | Measured: about 0.5 ms of vector search per query at 10,000 chunks and 6 ms at 100,000; reranking 5 candidates about 30 ms |

Vector search is exact, which stays comfortable to about 100,000 chunks per process. Beyond
that, plan the move to PostgreSQL with pgvector
([ADR-0002](decisions/0002-v0-storage-and-scope.md)).

## Streaming

`POST /v1/query/stream` answers with Server-Sent Events
([ADR-0010](decisions/0010-streaming-answers-over-sse.md)). Behind a reverse proxy:

- Don't buffer it. The response sends `X-Accel-Buffering: no`, which nginx honours; for
  other proxies, turn response buffering off for that path.
- Allow quiet periods. Before the first text, Claude may think for a while and nothing is
  sent; the proxy's read or idle timeout must be longer than `RAG_ANTHROPIC_TIMEOUT_S`.
- A client that disconnects stops the generation within one event, so abandoned answers
  aren't billed to the end.

## Rate limits

The API limits each client address per minute: by default 30 uploads and retries, 60
questions, 120 searches, 600 other `/v1` requests and 10 failed API-key attempts
([configuration.md](configuration.md#api-and-logging)). A client over a limit gets 429
`rate_limited` with `Retry-After`, and the API logs `security.rate_limited` with the operation
and the client address.

The limits are kept in each process's memory:

- With `--workers N`, each process keeps its own allowance, so a client can make up to N times
  a limit. Set each limit to about the total you want divided by N if the total must hold.
- A restart resets them.
- The client is the address uvicorn reports. Behind a reverse proxy, list the proxy in
  uvicorn's `--forwarded-allow-ips` so it uses `X-Forwarded-For`; otherwise every client
  shares the proxy's address and one allowance. IPv6 clients are grouped by /64.

For a limit that holds across processes or machines, rate-limit at the reverse proxy too; the
built-in limits are then a backstop in each process.

## Background ingestion

Uploads return `202` and are processed by worker threads inside each API process
(`RAG_INGEST_WORKERS`, default 1). With `--workers 4`, four processes each run one thread;
they share the queue through the database and never process the same document twice. Clients
poll `GET /v1/documents/{id}` until the status is `ready` or `failed`
([api.md](api.md#processing-status)). `rag ingest` doesn't use the queue: it processes each
file before moving on.

| Situation | What happens | Operator action |
| --- | --- | --- |
| Processing error (bad file, embedding failure) | `failed` with `error.code` and `error.message`; no chunks are written; the upload is kept | Fix the cause, then `POST /v1/documents/{id}/retry` (or re-upload). Errors are never retried automatically |
| Graceful shutdown (SIGTERM, deploy) | Workers stop at the next embedding batch and hand the document back as `queued`, without counting the attempt; shutdown waits up to 10 s | None: the next start picks it up |
| Crash, kill or power loss mid-way | The document stays `processing` until its lease lapses (`RAG_INGEST_LEASE_S`, default 5 min), then any worker takes it over | None. To recover sooner after a known crash, restart with a shorter lease |
| A file that hangs or exhausts the parser | The parser process is killed after `RAG_PARSE_TIMEOUT_S` (or hits `RAG_PARSE_MEMORY_MB` on Linux and macOS); the document fails with `parse_timeout` or `parse_failed`; the service is unaffected | Raise the limit if the file is legitimate, then retry |
| Interrupted `RAG_INGEST_MAX_ATTEMPTS` times (default 3) | `failed` with code `interrupted`, so a file that crashes the process can't loop forever | Check memory and the log for that document, then retry |
| No workers anywhere (`RAG_INGEST_WORKERS=0` in every process) | Uploads stay `queued` | Run at least one process with workers |
| Restart with work queued | Queued documents and their uploads are in the database; workers continue after startup | None |

Watch `/ready`: `checks.ingestion` gives documents by status and the live worker threads of the
process that answered. A growing `queued` count means workers can't keep up or aren't running;
`processing` that never drains points at a stuck or crashed worker. Failed documents keep their
upload in the database: delete the ones you won't retry.

Concurrent writes still go through SQLite's single writer. Workers hold the write lock only
briefly (claims, lease renewals) and once per document to write its chunks, and queries never
wait on it (WAL). Raising `RAG_INGEST_WORKERS` rarely helps: embedding already uses every CPU
core.

## Reranking

With `RAG_RERANKER=fastembed`, each query also runs a local cross-encoder over
`RAG_RERANK_CANDIDATES` passages (default 5; about 30 ms on a laptop CPU, or about 250 ms
with 20 candidates). Pre-download its model the same way as the embedding model
(`production_rag.retrieval.reranking.FastEmbedReranker`). After changing `RAG_RERANKER_MODEL`,
set `RAG_MIN_RERANK_SCORE` from a new calibration: only `Xenova/ms-marco-MiniLM-L-6-v2` has a
calibrated default, and other models never abstain on rerank score
([evaluation.md](evaluation.md#calibrating-abstention)).

## Changing the embedding model

Each chunk records the model that embedded it, and vector search uses only chunks from the
configured model. After changing `RAG_EMBEDDING_PROVIDER` or `RAG_FASTEMBED_MODEL`:

```bash
uv run rag reindex      # re-embeds every chunk from another model; prints counts
```

`/ready` reports `stale_chunks` until this has run. Lexical search keeps working in the
meantime. Re-run `rag eval` with the new model and recalibrate `RAG_MIN_VECTOR_SCORE`.

## Logs and monitoring

The backend emits three signals. [observability.md](observability.md) lists every log event
and metric, with Prometheus rules for the alerts below.

- **Metrics** at `GET /metrics`, in the Prometheus text format: request rates and latency by
  route, answer outcomes, stage latency, tokens, ingestion outcomes, queue depth and age,
  security refusals. Scrape every 15 seconds, with the API key when one is set. Counters are
  per process; the queue and index gauges come from the database.
- **Logs**: JSON lines on stderr, one event per line. Ship them to your log store and filter
  on `request_id` for a request, or `document_id` for a document's ingestion.
- **Probes**: `GET /health` for liveness and `GET /ready` for readiness.

Every log line within a request carries `request_id`, which is also returned as `X-Request-ID`
and in error bodies. `generation.completed` also logs `anthropic_request_id`; quote it when
reporting an API problem to Anthropic. Question text, document contents, answers and
credentials are never logged or used as metric labels.

### Alerts worth setting

Starting points; tune the thresholds to your traffic. Without Prometheus, the log events in
the last column give the same signal.

| Alert | Metric condition | From the logs | Usual cause |
| --- | --- | --- | --- |
| Not ready | `rag_database_up` or `rag_schema_at_head` is 0 for 2 minutes | `/ready` isn't 200 | Schema behind after an upgrade; database unreachable |
| Ingestion backlog | `rag_ingestion_oldest_queued_seconds` above 900 | `checks.ingestion.queued` keeps growing | No process runs workers, or workers fail (`ingest.worker_error`) |
| No workers | `rag_ingestion_workers` sums to 0 while `rag_documents{status="queued"}` is above 0 | `ingest.workers_started` missing after a restart | Started with `RAG_INGEST_WORKERS=0` everywhere, or the schema was behind at startup |
| Server errors | 5xx above 1% of `rag_http_requests_total` over 5 minutes | `http.request` with `status` 500 or more | Unexpected errors: look up the `request_id` |
| Model unavailable | `rag_generation_failures_total{retryable="true"}` rising | `generation.failed` with `retryable = true` | Claude rate limits, overload or an outage |
| Model misconfigured | any `rag_generation_failures_total{retryable="false"}` | `generation.failed` with `retryable = false` | Credentials, credit or the model name |
| Abuse | `rag_security_refusals_total` above 1 per second for 10 minutes | Spikes of `security.auth_failed` or `security.rate_limited` | A misconfigured client, or key guessing |
| Slow answers | p95 of `rag_query_stage_seconds{stage="total"}` above your target | p95 of `query.completed.timings_ms.total` | Large corpus, reranking many candidates, or a slow model |

## Backups

On PostgreSQL, back up with `pg_dump` or the provider's snapshots; this section is about
the SQLite file.

The database is one SQLite file in WAL mode, and the only copy of the extracted text and
the ingestion state. Copying the file alone can miss what is still in the write-ahead log, so
use `rag db backup`, which copies it with SQLite's backup API while the service runs:

```bash
rag db backup --keep 7                       # backups/ beside the database, newest 7 kept
rag db verify --file backups/rag-backup-20261110-043920-504.db
docker exec rag rag db backup --keep 7       # in the container: /data/backups
docker cp rag:/data/backups/. ./offsite/     # then copy them off the host, encrypted
```

Each backup is a single file with a JSON manifest beside it (time, version, schema
revision, counts, SHA-256). The copy passes SQLite's integrity check before it gets its final
name. `rag db verify` checks the live database, or a backup with `--file`, for damage and
broken invariants, and exits 1 if any check fails.

To restore, stop the service, then:

```bash
rag db restore backups/rag-backup-20261110-043920-504.db --yes
rag db upgrade                               # only if it reports "needs_upgrade": true
```

The restore refuses a backup that fails verification. It keeps the database it replaces as
`rag.pre-restore-<time>.db` and verifies the result. Start the service and check `/ready`.

For retention, `rag db prune --failed-older-than 30` deletes failed documents, with their
stored uploads, that nobody has retried for 30 days; add `--dry-run` to list them first. Run
both commands from a scheduler, such as cron or a Kubernetes CronJob.
[data-lifecycle.md](data-lifecycle.md) covers what each piece of data is, how long it is
kept, how to rebuild it and the recovery procedure for each kind of failure.

## PostgreSQL

SQLite is the default and the fastest choice for one instance. Use PostgreSQL (16 or later,
with pgvector 0.6 or later) to run several instances against one database, or to use the
backups and operations a team already has for it
([ADR-0013](decisions/0013-postgresql-with-pgvector.md)).

```bash
uv sync --frozen --extra postgres            # pg8000; the container image includes it
# once, as a superuser: the migrations create the extension
psql -c "CREATE DATABASE rag" -c "CREATE ROLE rag LOGIN PASSWORD '...'" \
     -c "ALTER DATABASE rag OWNER TO rag"
psql -d rag -c "CREATE EXTENSION IF NOT EXISTS vector"
export RAG_DATABASE_URL=postgresql+pg8000://rag:...@db.internal:5432/rag
rag db upgrade
```

- **Evaluation and the benchmark** create a throwaway database on the same server and drop
  it afterwards. Their role needs `CREATEDB`; run them with a separate role, not the
  service's.
- **Backups**: use `pg_dump` and `pg_restore`, or your provider's snapshots. `rag db backup`,
  `verify` and `restore` work on SQLite files and say so on PostgreSQL.
- **Several instances** can share the database: the queue's claims are transactions, so a
  document is processed once. Rate limits and the failed sign-in lockout are still per
  process; enforce limits at the load balancer too.
- **Speed**: each search is a query to the server. Measured at 10,000 chunks, retrieval p95
  was 95 ms, against 13 ms on SQLite in one process ([performance.md](performance.md)).
  Searches are exact and scan all of an owner's chunks; plan an approximate index past
  about 30,000 chunks per owner.
- **Moving from SQLite**: upload the documents again into the PostgreSQL deployment. There is
  no copy tool between the two.

## Accounts

To give several people their own documents, turn accounts on and create the first
administrator from the server's shell. The password is typed at a prompt, or read from
stdin with `--password-stdin`; it is never a command-line argument.

```bash
export RAG_ACCOUNTS_ENABLED=true          # not together with RAG_API_KEY
rag db upgrade
rag users create --admin ada@example.com
rag users list
docker exec -it rag rag users create --admin ada@example.com   # in the container
```

- The administrator creates other users with `POST /v1/users`, or `rag users create`.
  There is no sign-up page.
- A user who lost their password: `rag users set-password EMAIL`, or `PATCH /v1/users/{id}`.
  That signs them out everywhere.
- A user who should lose access: `rag users disable EMAIL`. It takes effect at once, for
  sessions and API tokens. `rag users delete EMAIL --yes` also deletes their documents.
- If every administrator is locked out, `rag users` on the server still works: it needs
  access to the database, not a sign-in.
- Serve over HTTPS: the session cookie is `Secure` and browsers won't send it over plain
  HTTP. For a test on `http://localhost`, set `RAG_SESSION_COOKIE_SECURE=false`.
- Scripts use personal API tokens (`POST /v1/auth/tokens`), sent as a bearer token.
- Documents uploaded before accounts were turned on belong to no user and aren't visible
  to anyone. Upload them again as the user who should own them.
- Turning accounts off again returns to the shared space; users' documents stay in the
  database but aren't visible there.

## Web interface

The container image serves the interface at `/`. From source, build it and set
`RAG_WEB_DIR`:

```bash
cd web && npm ci && npm run build      # writes web/dist
RAG_WEB_DIR=web/dist rag serve
```

- The page loads without credentials. With accounts it asks for an email address and
  password; with `RAG_API_KEY` it asks for the key and keeps it for the browser tab.
- It calls its own origin, so serve it and the API from one host name. Behind a proxy,
  forward `/`, `/assets/` and the API paths to the service, and keep buffering off for
  `/v1/query/stream` ([Streaming](#streaming)).
- `index.html` is revalidated on each load, and the files under `/assets/` are cached for a
  year under names that change with their content, so an upgrade needs no cache clearing.
- To serve the files from the proxy instead, point it at `web/dist` and leave
  `RAG_WEB_DIR` unset.
- For development, `npm run dev` in `web/` serves the interface with hot reload and
  forwards API calls to `http://127.0.0.1:8000` (`RAG_API_URL` changes that).

## Security

Security controls, data flows to the model provider, known gaps and an operator
checklist are in [security.md](security.md). All settings are listed in
[configuration.md](configuration.md).

## Troubleshooting

Quick causes and fixes are below. For an incident, [runbooks.md](runbooks.md) has the
severity levels, the first steps, a procedure for each kind of failure, rollback and how to
confirm recovery.

| Symptom | Cause and fix |
| --- | --- |
| `/ready` 503 with `schema: not at head` | Run `rag db upgrade` |
| `/` answers 404 | `RAG_WEB_DIR` is unset, or has no `index.html` (`web.not_found` in the logs): build the interface, or use the container image |
| `rag db verify` fails `search_index`, or searches miss text that is in a document | The full-text index is out of step with the chunks: `rag db rebuild-search-index` |
| `rag db verify` fails `sqlite_integrity`, or SQLite reports "database disk image is malformed" | Stop the service and restore the newest backup that verifies ([data-lifecycle.md](data-lifecycle.md#recovery)) |
| Documents stay `queued` | No process runs workers (`checks.ingestion.workers` is 0 everywhere), or workers log `ingest.worker_error`. Start a process with `RAG_INGEST_WORKERS` ≥ 1, or fix the database error |
| A document stays `processing` for minutes | Its worker died; it's taken over once the lease lapses (`RAG_INGEST_LEASE_S`). Large PDFs can also simply take that long: check for `ingest.completed` |
| `failed` with `internal_error` | The message names the exception type; the full error is in the `ingest.failed` log line. Fix it, then retry |
| HTTP 502 `generation_failed`, "no Claude API credentials configured" | Set `ANTHROPIC_API_KEY` (read by the SDK, without the `RAG_` prefix) |
| "the Claude API account has no credit left" (HTTP 502 `generation_failed`, or from `rag eval --plan --count-tokens`) | The key authenticates but the account's credit balance is empty; even token counting is refused. Add credits under Plans & Billing in the Claude Console |
| HTTP 503 `generation_unavailable` | Rate limit, overload or network failure after SDK retries; retry later, check `generation.failed` logs |
| Many `insufficient_evidence` answers | Check `best_vector_score` in `query.completed`: if it's low, the corpus may lack the content; if retrieval looks right, run `rag eval` and recalibrate the threshold |
| fastembed fails with "Could not load model … from any source" | Often not the network: Hugging Face downloads large files through its Xet client, which writes to `~/.cache/huggingface`. If that isn't writable, set `HF_HOME` and `RAG_FASTEMBED_CACHE_DIR` to writable directories |
| Streamed answers arrive all at once, at the end | A proxy buffers the response. Turn buffering off for `/v1/query/stream` ([Streaming](#streaming)) |
| Clients get 429 `rate_limited` | Over a per-client limit; `Retry-After` says when to try again. Behind a proxy that isn't in `--forwarded-allow-ips`, all clients share one allowance. Raise `RAG_RATE_LIMIT_*_PER_MINUTE` if the limits are too low ([Rate limits](#rate-limits)) |
| The container can't write to `/data` | A bind-mounted host directory owned by another user: the image runs as uid 10001. `chown 10001` the directory, or use a named volume |
| `database is locked` | Concurrent writers beyond what SQLite handles; reduce concurrent ingestion or move to PostgreSQL ([ADR-0002](decisions/0002-v0-storage-and-scope.md)) |
| Windows: "An Application Control policy has blocked this file" for `pytest.exe` | OS policy on unsigned launcher shims; run `uv run python -m pytest` instead |
