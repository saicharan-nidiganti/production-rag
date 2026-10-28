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
| 413 | `document_too_large` | Upload over `RAG_MAX_UPLOAD_BYTES` |
| 415 | `unsupported_document_type` | Extension not supported, or a `.pdf` whose content isn't a PDF |
| 422 | `extraction_failed` | No extractable text, not UTF-8, encrypted or corrupt PDF, too many pages |
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
    "schema_revision": "0001",
    "expected_revision": "0001",
    "documents": 1,
    "chunks": 5,
    "stale_chunks": 0
  }
}
```

`stale_chunks` counts chunks embedded by a model other than the configured one.
Run `rag reindex` to fix them.

## Documents

### `POST /v1/documents`

Multipart upload with one `file` field. Supported: `.txt`, `.md`, `.markdown`,
`.pdf` (with a text layer). Returns **201** for new content, or **200** with
`created: false` when identical bytes were already ingested.

```bash
curl -s localhost:8000/v1/documents -F "file=@eval/corpus/api-rate-limits.md"
```

```json
{
  "document": {
    "id": "cb006f27-b49b-4798-b70c-cb6d8d900c30",
    "source_name": "api-rate-limits.md",
    "title": "Public API Usage Limits",
    "content_type": "text/markdown",
    "size_bytes": 1275,
    "num_chunks": 5,
    "content_hash": "1d850177f624841c75767edcdeeb582aa45bc9e19949825050ec43fddb46f303",
    "metadata": {},
    "created_at": "2026-10-29T10:00:00Z"
  },
  "created": true
}
```

The title is the first Markdown H1, the PDF title metadata, or the file name.
For PDFs, `metadata.pages` holds the page count.

### `GET /v1/documents?limit=50&offset=0`

Newest first. Returns `{"items": [document, ...], "total", "limit", "offset"}`.
`limit` is 1–200.

### `GET /v1/documents/{id}` and `DELETE /v1/documents/{id}`

Fetch one document, or delete it with all its chunks and index entries (204).

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
      "lexical_score": 3.398386965838204
    }
  ],
  "best_vector_score": 0.27024319767951965,
  "timings_ms": {"retrieval": 5.13}
}
```

`score` is the ranking score for the mode: cosine for `vector`, BM25 for
`lexical`, RRF for `hybrid`. `start_char` and `end_char` locate the chunk in the
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
