# Operations

Running the backend outside a development laptop.

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

## Deploy sequence

1. `uv run rag db upgrade`: apply migrations. This is an explicit step; the service doesn't
   migrate on startup. `/ready` returns 503 until the schema is at head.
2. Start the API: `uv run uvicorn --factory production_rag.api.app:app_factory --host 0.0.0.0
   --port 8000 --workers 4`, or `uv run rag serve --host 0.0.0.0` for a single process.
3. Point the liveness probe at `GET /health` and the readiness probe at `GET /ready`.

Run several workers on one machine if you need to. Each keeps its own vector cache, and caches
stay consistent through the `index_state` generation counter. SQLite allows a single writer, so
don't share the database file across machines or over network filesystems
([ADR-0002](decisions/0002-v0-storage-and-scope.md) lists when to move to PostgreSQL).

Put a reverse proxy in front for TLS and to cap request bodies at `RAG_MAX_UPLOAD_BYTES`. The
app enforces the limit too, but only after the body has been received.

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

Logs are JSON lines on stderr. Useful fields and signals:

| Signal | Source |
| --- | --- |
| Request rate, latency and errors by path | `event = http.request` (`status`, `duration_ms`, `path`) |
| Answer outcomes | `event = query.completed`: count by `status`; watch the `insufficient_evidence` share |
| Pre-generation abstentions | `query.completed` with `abstained_before_generation = true` |
| Stage latency | `query.completed.timings_ms.retrieval` / `.generation` |
| Model usage and cost | `event = generation.completed`: `input_tokens`, `output_tokens`, `model` |
| Refusals | `event = generation.refused` (`category`) |
| Upstream failures | `event = generation.failed` (`retryable`); HTTP 502/503 |

Every log line within a request carries `request_id`, which is also returned as `X-Request-ID`.
`generation.completed` also logs `anthropic_request_id`; quote it when reporting an API problem
to Anthropic. Question text and document contents are never logged.

## Backups

The database is a single SQLite file in WAL mode. Take consistent backups with the SQLite backup
API or `sqlite3 rag.db ".backup 'rag-backup.db'"` (copying the file alone can miss WAL
contents). Documents can be re-ingested from source, but the database is the only copy of
extracted text and ingestion metadata.

## Security

Security controls, data flows to the model provider, known gaps and an operator
checklist are in [security.md](security.md). All settings are listed in
[configuration.md](configuration.md).

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `/ready` 503 with `schema: not at head` | Run `rag db upgrade` |
| HTTP 502 `generation_failed`, "no Claude API credentials configured" | Set `ANTHROPIC_API_KEY` (read by the SDK, without the `RAG_` prefix) |
| "the Claude API account has no credit left" (HTTP 502 `generation_failed`, or from `rag eval --plan --count-tokens`) | The key authenticates but the account's credit balance is empty; even token counting is refused. Add credits under Plans & Billing in the Claude Console |
| HTTP 503 `generation_unavailable` | Rate limit, overload or network failure after SDK retries; retry later, check `generation.failed` logs |
| Many `insufficient_evidence` answers | Check `best_vector_score` in `query.completed`: if it's low, the corpus may lack the content; if retrieval looks right, run `rag eval` and recalibrate the threshold |
| fastembed fails with "Could not load model … from any source" | Often not the network: Hugging Face downloads large files through its Xet client, which writes to `~/.cache/huggingface`. If that isn't writable, set `HF_HOME` and `RAG_FASTEMBED_CACHE_DIR` to writable directories |
| `database is locked` | Concurrent writers beyond what SQLite handles; reduce concurrent ingestion or move to PostgreSQL ([ADR-0002](decisions/0002-v0-storage-and-scope.md)) |
| Windows: "An Application Control policy has blocked this file" for `pytest.exe` | OS policy on unsigned launcher shims; run `uv run python -m pytest` instead |
