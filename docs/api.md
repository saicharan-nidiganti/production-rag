# HTTP API

The API is served by `uv run rag serve` (or uvicorn, see
[operations.md](operations.md#deploy-sequence)). FastAPI publishes the
machine-readable schema at `/openapi.json` and interactive documentation at
`/docs`. The examples below are real responses from the offline configuration
(`hashing` embeddings, `extractive` generator), shortened where marked `...`.

## Conventions

- **Authentication:** when `RAG_API_KEY` is set, every `/v1/*` request needs
  `Authorization: Bearer <key>` or `X-API-Key: <key>`. `/health` and `/ready`
  are always open, for probes.
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

  Validation errors add `details`, the list of field-level problems.

| Status | `code` | When |
| --- | --- | --- |
| 401 | `unauthorized` | Missing or wrong API key |
| 404 | `not_found` | Unknown document or route |
| 409 | `invalid_document_state` | Retrying a document that hasn't failed |
| 413 | `document_too_large` | Upload over `RAG_MAX_UPLOAD_BYTES` |
| 415 | `unsupported_document_type` | Extension not supported, or a `.pdf` whose content isn't a PDF |
| 422 | `extraction_failed` | Upload is empty or not UTF-8 text. Problems found while parsing are reported on the document instead ([processing status](#processing-status)) |
| 422 | `validation_error` | Request body or parameters invalid (see `details`) |
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

## Documents

### `POST /v1/documents`

Multipart upload with one `file` field. Supported: `.txt`, `.md`, `.markdown`,
`.pdf` (with a text layer). The upload is checked and stored, then processed in
the background (FR-3):

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
    "updated_at": "2026-10-29T03:54:07.870544Z"
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
the page count.

`GET /v1/documents/{id}` once processing has finished:

```json
{
  "id": "37f7f8c9-44cd-463d-ab2d-94778be20085",
  "title": "Public API Usage Limits",
  "num_chunks": 5,
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
| `unsupported_document_type` | The content doesn't match its type | Upload the right file |
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
