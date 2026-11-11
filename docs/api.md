# HTTP API

The API is served by `uv run rag serve` (or uvicorn, see
[operations.md](operations.md#deploy-sequence)). FastAPI publishes the
machine-readable schema at `/openapi.json` and interactive documentation at
`/docs`. The examples below are real responses from the offline configuration
(`hashing` embeddings, `extractive` generator), shortened where marked `...`.

## Conventions

- **Authentication:** with `RAG_ACCOUNTS_ENABLED`, see [Accounts](#accounts). Otherwise,
  when `RAG_API_KEY` is set, every request needs
  `Authorization: Bearer <key>` or `X-API-Key: <key>`, except `/health` and
  `/ready` (for probes) and the API docs (`/docs`, `/redoc`, `/openapi.json`).
  The key is checked before the request body is read, so an unauthenticated
  upload is refused without being received.
- **Request size:** uploads may be up to `RAG_MAX_UPLOAD_BYTES` (plus 64 KiB of
  multipart framing), other bodies up to `RAG_MAX_REQUEST_BYTES` (64 KiB). A
  larger `Content-Length` is refused with 413 before the body is read; a body
  sent without one is refused when it reaches the limit.
- **Rate limits:** each client address has a per-minute allowance for uploads
  and retries, questions, searches, other `/v1` requests and failed API-key
  attempts (`RAG_RATE_LIMIT_*` in [configuration.md](configuration.md#api-and-logging)).
  Over one, the request is refused with 429 `rate_limited` before its body is
  read, and `Retry-After` gives the seconds to wait. `/health`, `/ready` and
  the API docs are never limited.
- **Versioning:** endpoints live under `/v1`; `/health` and `/ready` are
  unversioned. Within a version, changes are additive only: new endpoints,
  new optional request fields and new response fields, so clients should
  ignore fields they don't know. Removing or renaming a field, changing its
  type or meaning, or rejecting requests that used to be accepted needs a new
  version (`/v2`), served alongside the old one for a deprecation period
  announced in [CHANGELOG.md](../CHANGELOG.md). Until the first release
  (`0.x`), `/v1` may still change incompatibly, and every such change is
  listed in the changelog: uploads, for example, moved from `201` to `202`
  with background ingestion. `/openapi.json` always describes the running
  server.
- **Request ids:** send `X-Request-ID` (1–64 characters from `A-Z a-z 0-9 . _ -`)
  to correlate logs, or let the server generate one. Every response carries
  `X-Request-ID`, and every log line for the request includes it.
- **Errors** use one envelope:

  ```json
  {"error": {"code": "not_found", "message": "document … not found", "request_id": "…"}}
  ```

  Validation errors add `details`, the list of field-level problems (`loc`,
  `msg`, `type`; the rejected input itself is not echoed back).

| Status | `code` | When |
| --- | --- | --- |
| 401 | `unauthorized` | Missing or wrong API key |
| 404 | `not_found` | Unknown document or route |
| 409 | `invalid_document_state` | Retrying a document that hasn't failed |
| 413 | `document_too_large` | Upload over `RAG_MAX_UPLOAD_BYTES` |
| 413 | `request_too_large` | Any other request body over `RAG_MAX_REQUEST_BYTES` |
| 415 | `unsupported_document_type` | Extension not supported, or a `.pdf` whose content isn't a PDF |
| 422 | `extraction_failed` | Upload is empty or not UTF-8 text. Problems found while parsing are reported on the document instead ([processing status](#processing-status)) |
| 422 | `validation_error` | Request body or parameters invalid (see `details`) |
| 429 | `rate_limited` | Over a rate limit for this client; wait `Retry-After` seconds |
| 502 | `generation_failed` | The model provider rejected the request: bad or missing credentials, unknown model |
| 503 | `generation_unavailable` | Rate limit, overload or network failure after retries; retry later |
| 500 | `internal_error` | Unexpected failure. The message is generic; use the request id to find the log line. |

## Health

### `GET /health`

Liveness. Checks no dependencies. Returns `{"status": "ok"}`.

### `GET /ready`

Readiness: the database is reachable and its schema is at the latest
migration. Returns 200, or 503 with `"status": "not_ready"`:

```json
{
  "status": "ready",
  "checks": {
    "embedding_model": "hashing-v1-d512",
    "schema_revision": "0002",
    "expected_revision": "0002",
    "documents": 2,
    "chunks": 5,
    "stale_chunks": 0,
    "ingestion": {"queued": 0, "processing": 0, "ready": 1, "failed": 1, "workers": 1}
  }
}
```

`stale_chunks` counts chunks embedded by a model other than the configured one.
Run `rag reindex` to fix them. `ingestion` counts documents by status and the
live ingestion threads in the process that answered. It's for monitoring and
doesn't affect readiness.

### `GET /metrics`

Prometheus metrics in the text exposition format (`text/plain; version=0.0.4`): request,
question, generation, ingestion and security counters for the process that answered, and
the state of the queue and index read from the database. Unlike the probes it requires the
API key when one is set, and it draws from the `read` rate limit. Returns 404 `not_found`
when `RAG_METRICS_ENABLED=false`. Every metric is listed in
[observability.md](observability.md#metrics).

```text
rag_http_requests_total{method="POST",route="/v1/query",status="200"} 12.0
rag_queries_total{status="answered",streamed="false"} 9.0
rag_query_stage_seconds_bucket{le="0.5",stage="total"} 11.0
rag_documents{status="ready"} 4.0
rag_ingestion_oldest_queued_seconds 0.0
```

## Accounts

With `RAG_ACCOUNTS_ENABLED=true`, every `/v1` request acts as a user, and documents,
searches and answers are limited to that user's own documents
([ADR-0012](decisions/0012-accounts-and-per-user-isolation.md)). Another user's document
answers 404, exactly like one that doesn't exist. With accounts off, every route in this
section except `GET /v1/auth/status` answers 404.

A request is authenticated in one of two ways:

- **A browser session.** `POST /v1/auth/login` sets the `rag_session` cookie (`HttpOnly`,
  `SameSite=Strict`, `Secure`). Requests that change something (`POST`, `PATCH`, `DELETE`)
  must also send the session's CSRF token as `X-CSRF-Token`, or get 403 `csrf_failed`.
- **A personal API token**, sent as `Authorization: Bearer rag_…`. No CSRF token is needed.

| Route | Who | What it does |
| --- | --- | --- |
| `GET /v1/auth/status` | Anyone | `{"accounts_enabled", "user", "csrf_token"}`. Never 401: `user` is null when nobody is signed in |
| `POST /v1/auth/login` | Anyone | Body `{"email", "password"}`. 200 with the same shape as `status`, and the cookie. 401 for a wrong password, an unknown address or a disabled account, all with the same message. Failures count against the client's allowance (`RAG_RATE_LIMIT_AUTH_FAILURES_PER_MINUTE`); past it, 429 |
| `POST /v1/auth/logout` | A user | Ends the session. 204 |
| `POST /v1/auth/password` | A session | Body `{"current_password", "new_password"}`. 204, and every other session of the user is signed out. 403 if the current password is wrong; 422 if the new one is too short |
| `GET /v1/auth/tokens` | A user | The user's tokens: `id`, `name`, `created_at`, `last_used_at`. Never the token itself |
| `POST /v1/auth/tokens` | A session | Body `{"name"}`. 201 with `token`, shown this once. A token can't create tokens |
| `DELETE /v1/auth/tokens/{id}` | A user | Revokes one of the user's own tokens. 204, or 404 |
| `GET /v1/users` | An administrator | Every account: `id`, `email`, `role`, `is_active`, `created_at` |
| `POST /v1/users` | An administrator | Body `{"email", "password", "role"}`. 201; 409 if the address is taken; 422 for a bad address or password |
| `PATCH /v1/users/{id}` | An administrator | Any of `{"is_active", "role", "password"}`. Disabling signs the user out. 409 for the last working administrator |
| `DELETE /v1/users/{id}` | An administrator | Deletes the account **and every document it owns**. 204; 409 for yourself or the last working administrator |

```bash
curl -s -c jar localhost:8000/v1/auth/login -H "Content-Type: application/json" \
  -d '{"email": "ada@example.com", "password": "..."}'      # note csrf_token in the reply
curl -s -b jar -H "X-CSRF-Token: $CSRF" localhost:8000/v1/auth/tokens \
  -H "Content-Type: application/json" -d '{"name": "nightly import"}'
curl -s -H "Authorization: Bearer rag_..." localhost:8000/v1/documents
```

An administrator manages accounts but can't read other users' documents. `/metrics` needs an
administrator. `/health` and `/ready` stay open.

## Documents

### `POST /v1/documents`

Multipart upload with one `file` field. Supported: `.txt`, `.md`, `.markdown`,
`.pdf` (with a text layer), `.docx`, `.html` and `.htm`. Text and HTML must be
UTF-8; HTML may instead declare its encoding in a `<meta>` tag. Legacy `.doc`,
password-protected and macro-enabled Word files are refused. The upload is
checked and stored, then processed in the background (FR-3):

- **202** with `created: true`: new content, queued. `Location` points at the
  document; poll it until `status` is `ready` or `failed`.
- **202** with `created: false`: identical bytes were submitted before and
  aren't processed yet, or had failed and are queued again.
- **200** with `created: false`: identical bytes are already `ready`.
- **413**, **415**, **422**: the checks that need no parsing (size, type, PDF
  signature, UTF-8, empty text) reject the upload outright. Nothing is stored.

```bash
curl -si localhost:8000/v1/documents -F "file=@eval/corpus/api-rate-limits.md"
```

**A new version of a document.** Add `?replaces={id}` to name the document an upload
replaces. The old version is deleted in the transaction that marks the new one `ready`,
so a search finds the old version until then and the new one from then on, never both or
neither. If the new version fails, the old one stays, and a retry that succeeds replaces it
then. The new document's `replaces` field keeps the old id.

- **404** `not_found`: no document has that id. Nothing is stored.
- If the bytes match a document that is already `ready`, the old version is deleted at once
  and the matching document is returned (200, `created: false`). If they match one that
  isn't ready yet, that one becomes the replacement.
- **409** `invalid_document_state`: the bytes match a document already uploaded as the
  replacement of a different document.

```bash
curl -s "localhost:8000/v1/documents?replaces=37f7f8c9-44cd-463d-ab2d-94778be20085" \
  -F "file=@api-rate-limits-v2.md"
```

```http
HTTP/1.1 202 Accepted
location: /v1/documents/37f7f8c9-44cd-463d-ab2d-94778be20085

{
  "document": {
    "id": "37f7f8c9-44cd-463d-ab2d-94778be20085",
    "source_name": "api-rate-limits.md",
    "title": "api-rate-limits",
    "content_type": "text/markdown",
    "size_bytes": 1275,
    "num_chunks": 0,
    "content_hash": "1d850177f624841c75767edcdeeb582aa45bc9e19949825050ec43fddb46f303",
    "metadata": {},
    "status": "queued",
    "error": null,
    "attempts": 0,
    "created_at": "2026-10-29T03:54:07.870544Z",
    "updated_at": "2026-10-29T03:54:07.870544Z",
    "replaces": null
  },
  "created": true
}
```

### Processing status

| `status` | Meaning | Next |
| --- | --- | --- |
| `queued` | Stored, waiting for a worker | `processing` |
| `processing` | A worker is extracting, chunking and embedding it | `ready`, `failed`, or `queued` again if the server shuts down mid-way |
| `ready` | Chunks are indexed and searchable | final |
| `failed` | `error` says why; nothing was indexed | `queued` on retry or re-upload |

Only `ready` documents appear in search and answers. Until then, `title` is the
file name and `num_chunks` is 0. Once ready, the title is the first Markdown H1,
the PDF title metadata, or the file name, and for PDFs `metadata.pages` holds
the page count. `metadata.ingested_with` records what produced the chunks: the
version and the chunk size and overlap ([data-lifecycle.md](data-lifecycle.md#lineage)).

`GET /v1/documents/{id}` once processing has finished:

```json
{
  "id": "37f7f8c9-44cd-463d-ab2d-94778be20085",
  "title": "Public API Usage Limits",
  "num_chunks": 5,
  "metadata": {"ingested_with": {"version": "0.2.0", "chunk_size": 1000, "chunk_overlap": 150}},
  "status": "ready",
  "error": null,
  "attempts": 1,
  "created_at": "2026-10-29T03:54:07.870544Z",
  "updated_at": "2026-10-29T03:54:07.890220Z",
  ...
}
```

A failure carries a code and a message written for the person who uploaded it:

```json
{
  "id": "5eb40076-dcb3-45cc-b930-1fa1cdc9d42b",
  "source_name": "notes.md",
  "num_chunks": 0,
  "status": "failed",
  "error": {"code": "extraction_failed", "message": "document contains no extractable text"},
  "attempts": 1,
  ...
}
```

| `error.code` | Cause | What to do |
| --- | --- | --- |
| `extraction_failed` | No text after parsing, encrypted or corrupt PDF, too many pages | Fix the file and upload it again |
| `unsupported_document_type` | The content doesn't match its type, or a `.docx` is macro-enabled | Upload the right file |
| `interrupted` | The worker stopped mid-way `RAG_INGEST_MAX_ATTEMPTS` times (crash, restart, out of memory) | Retry; if it recurs, check the server log for this document |
| `internal_error` | Unexpected failure, for example the embedding model couldn't load. The message names only the error type | Fix the cause (server log), then retry |
| `upload_missing` | The stored upload is gone | Upload the document again |
| `parse_timeout` | Parsing took longer than `RAG_PARSE_TIMEOUT_S` | Check the file; if it's legitimate, raise the limit and retry |
| `parse_failed` | The parser process crashed or ran out of memory (`RAG_PARSE_MEMORY_MB`) | As above |

`attempts` counts processing attempts since the document was last queued. An
attempt cut short by a crash counts; one handed back at shutdown doesn't.

### `POST /v1/documents/{id}/retry`

Queues a `failed` document again with its stored upload. Returns **202** with
the document (`status: queued`, `error: null`, `attempts: 0`), **404** for an
unknown id, or **409** `invalid_document_state` in any other status:

```json
{"error": {"code": "invalid_document_state", "message": "only failed documents can be retried; this one is ready", "request_id": "…"}}
```

Uploading identical bytes again has the same effect as a retry.

### `GET /v1/documents?limit=50&offset=0&status=failed`

Newest first. Returns `{"items": [document, ...], "total", "limit", "offset"}`.
`limit` is 1–200. `status` (optional) keeps one status; `total` counts the
filtered set.

### `GET /v1/documents/{id}` and `DELETE /v1/documents/{id}`

Fetch one document, or delete it, in any status, with its stored upload, chunks
and index entries (204). Deleting a document while it's being processed
discards that work.

## Retrieval

### `POST /v1/search`

| Field | Type | Default |
| --- | --- | --- |
| `query` | string, 1–2000 characters | required |
| `top_k` | integer, 1–50 | `RAG_TOP_K` |
| `mode` | `vector`, `lexical` or `hybrid` | `RAG_RETRIEVAL_MODE` |

```json
{
  "mode": "hybrid",
  "hits": [
    {
      "chunk_id": 3,
      "chunk_key": "f5fbe50623bae00f1afdde1e88aabe06",
      "document_id": "cb006f27-b49b-4798-b70c-cb6d8d900c30",
      "document_title": "Public API Usage Limits",
      "source_name": "api-rate-limits.md",
      "heading": "Public API Usage Limits > Handling 429 responses",
      "page": null,
      "text": "## Handling 429 responses\n\nWhen a client exceeds its rate limit, the API respond...",
      "start_char": 417,
      "end_char": 742,
      "score": 0.03278688524590164,
      "vector_score": 0.27024319767951965,
      "lexical_score": 3.398386965838204,
      "rerank_score": null
    }
  ],
  "best_vector_score": 0.27024319767951965,
  "best_rerank_score": null,
  "timings_ms": {"retrieval": 5.13}
}
```

`score` is the final ranking score: the cross-encoder score when a reranker
runs (`RAG_RERANKER`), otherwise cosine for `vector`, BM25 for `lexical` and RRF
for `hybrid`. `rerank_score` and `best_rerank_score` are `null` without a
reranker; with one, `timings_ms.rerank` reports the reranking time, which is
included in `timings_ms.retrieval`. `start_char` and `end_char` locate the chunk in the
document's extracted text.

Use `chunk_key`, not `chunk_id`, to refer to a passage over time. `chunk_id` is a database
row id: it changes when a document is deleted and uploaded again, and SQLite may reuse it for
another chunk. `chunk_key` is derived from the document's content hash and the chunk's
character span, so identical bytes chunked with the same `RAG_CHUNK_SIZE` and
`RAG_CHUNK_OVERLAP` always get the same keys. Changed content gets new keys, and changing the
chunk settings changes the keys of every chunk whose span moves.

## Question answering

### `POST /v1/query`

| Field | Type | Default |
| --- | --- | --- |
| `question` | string, 1–2000 characters | required |
| `top_k` | integer, 1–20 | `RAG_TOP_K` |
| `mode` | `vector`, `lexical` or `hybrid` | `RAG_RETRIEVAL_MODE` |

```json
{
  "status": "answered",
  "answer": "When a client exceeds its rate limit, the API responds with HTTP status 429 and a `Retry-After` header giving the number of seconds to wait. Requests rejected with 429 do not count against the monthly quota.",
  "segments": [
    {"text": "When a client exceeds its rate limit, ... the number of seconds to wait. ", "citations": [0]},
    {"text": "Requests rejected with 429 do not count against the monthly quota.", "citations": [1]}
  ],
  "citations": [
    {
      "chunk_id": 3,
      "chunk_key": "f5fbe50623bae00f1afdde1e88aabe06",
      "document_id": "cb006f27-b49b-4798-b70c-cb6d8d900c30",
      "document_title": "Public API Usage Limits",
      "source_name": "api-rate-limits.md",
      "heading": "Public API Usage Limits > Handling 429 responses",
      "page": null,
      "start_char": 417,
      "end_char": 742,
      "cited_text": "When a client exceeds its rate limit, the API responds with HTTP status 429 and a `Retry-A..."
    },
    {"chunk_id": 3, "...": "...", "cited_text": "Requests rejected with 429 do not count against the monthly quota."}
  ],
  "retrieved": ["..."],
  "model": "extractive-v1",
  "stop_reason": "end_turn",
  "usage": null,
  "timings_ms": {"retrieval": 1.38, "generation": 0.12, "total": 1.52}
}
```

- `status`: `answered` (at least one citation), `insufficient_evidence`
  (nothing relevant retrieved, or the model gave no cited answer) or `refused`
  (the model declined). Only `answered` should be presented as an answer.
- `segments` concatenate to `answer`. Each segment's `citations` are indexes
  into `citations`, for rendering inline markers.
- `retrieved` holds the chunks given to the generator, in the same shape as
  search hits.
- `usage` holds token counts when Claude generated the answer.
- `timings_ms.generation` is absent when the system abstained before
  generation.

With `RAG_GENERATOR=anthropic`, `model` is the Claude model that answered, and
citations come from Claude's search-result citations
([ADR-0003](decisions/0003-generation-provider-claude.md)).

### `POST /v1/query/stream`

The same request as `POST /v1/query`, answered as [Server-Sent
Events](https://html.spec.whatwg.org/multipage/server-sent-events.html) while
the answer is written (FR-9, [ADR-0010](decisions/0010-streaming-answers-over-sse.md)).
Each event is an `event:` line naming it and one `data:` line of JSON:

| Event | When | `data` |
| --- | --- | --- |
| `retrieval` | Once, first | `retrieved` (search hits) and `abstained_before_generation` |
| `text` | Any number | `text`: more answer text, to append |
| `citation` | Any number | `index` (its position in `done`'s `citations`) and `citation` |
| `done` | Once, last | The `POST /v1/query` response, exactly |
| `error` | Instead of `done` | The error envelope: generation failed after the stream started |

```bash
curl -sN "$RAG_URL/v1/query/stream" -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" -d '{"question": "How long does the canary run?"}'
```

A real stream from the offline configuration, abridged:

```text
event: retrieval
data: {"retrieved": ["..."], "abstained_before_generation": false}

event: citation
data: {"index": 0, "citation": {"chunk_id": 3, "source_name": "deployment-runbook.md", "heading": "Deployment Runbook > Canary releases", "...": "..."}}

event: text
data: {"text": "Production rollouts start with a canary. "}

event: citation
data: {"index": 1, "citation": {"chunk_id": 3, "...": "..."}}

event: text
data: {"text": "The canary receives 5% of production traffic for 30 minutes. "}

event: done
data: {"status": "answered", "answer": "Production rollouts start with a canary. The canary receives...", "...": "...", "timings_ms": {"retrieval": 3.79, "first_token": 5.73, "generation": 2.61, "total": 7.66}}
```

- **`done` is authoritative.** When its `status` is `refused`, discard the
  streamed text and show `done.answer` instead: the model declined partway and
  no fallback model completed the answer. When the model declines and a
  fallback model takes over (`RAG_ANTHROPIC_FALLBACKS`), the streamed text is
  kept and the fallback continues it, so no special handling is needed.
- `done.segments` and `done.citations` give the inline citation markers, as for
  `/v1/query`. `done.timings_ms.first_token` is the time from the request to
  the first text.
- **Errors.** Validation, authentication, rate limits and retrieval failures
  are ordinary HTTP errors, before the stream starts. Once it has started, a
  generation failure is an `error` event with `generation_unavailable`
  (retryable) or `generation_failed`, and an unexpected one with
  `internal_error`, each with the request id. Nothing follows an `error`.
- The stream counts against the question rate limit
  (`RAG_RATE_LIMIT_QUERY_PER_MINUTE`), like `/v1/query`.
- Closing the connection stops the generation, so an abandoned answer isn't
  generated, or billed, to the end.
- Responses carry `Cache-Control: no-cache` and `X-Accel-Buffering: no`; a
  reverse proxy must pass the stream through unbuffered
  ([operations.md](operations.md#streaming)).
