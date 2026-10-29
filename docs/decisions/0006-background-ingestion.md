# ADR-0006: Background ingestion on a SQLite-backed queue

- **Status:** Accepted, 2026-10-29. An engineering decision implementing FR-3,
  which the owner made the next milestone, within ADR-0002 (SQLite for v0).
- **Date:** 2026-10-29
- **Deciders:** Engineering, at the owner's direction
- **Supersedes:** [ADR-0002](0002-v0-storage-and-scope.md) decision 3
  (synchronous ingestion inside the upload request). It implements, on SQLite,
  the database-backed queue that [ADR-0001](0001-technology-stack.md) §9 chose
  for v1.
- **Amended 2026-10-29:** parsing now runs in a separate, resource-limited
  process ([ADR-0007](0007-isolated-document-parsing.md)), as "Revisit when"
  below anticipated.

## Context

Until now an upload was extracted, chunked and embedded inside the request.
Large PDFs held the request open for as long as that took, nothing recorded a
document's progress, and a failure left no trace to show or retry. FR-3 asks
for a per-document status (`queued`, `processing`, `ready`, `failed`) with an
actionable reason, and for failed documents to be retryable.

Constraints: SQLite stays the v0 store (D-3), so PostgreSQL's
`SELECT … FOR UPDATE SKIP LOCKED` isn't available. The API may run as several
uvicorn processes on one machine. Restarts and crashes must not lose accepted
uploads or leave documents stuck. There is no Redis or other broker to operate.

## Decision drivers

1. Uploads accepted with `202` must survive restarts and crashes.
2. No document may be processed twice at the same time, across threads and
   processes, and an interrupted document must not be left half-indexed (NFR-5).
3. Nothing new to deploy or operate in v0.
4. The design should carry over to ADR-0001's PostgreSQL queue.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| FastAPI `BackgroundTasks` | No code to speak of | Lost on restart; no status, retries or concurrency control |
| Celery, RQ or Arq with Redis | Mature, with retries and monitoring | A broker and a worker service to run; status still has to be stored with the document |
| **The `documents` table as the queue, claimed by compare-and-set, with leases; worker threads in each API process** ✅ | Durable by construction; status *is* the queue state; no new infrastructure; works with any number of processes | Polling; SQLite's single writer; parsing shares the API process |
| Same queue, separate `rag worker` process | Isolates parsing from serving | Another process to deploy and supervise for v0 |

## Decision

1. **State on the document.** `documents` gains `status`, `error_code`,
   `error_message`, `attempts`, `claim_token`, `lease_expires_at` and
   `updated_at` (migration 0002; existing documents become `ready`). The
   uploaded bytes live in `document_files` until the document is ready, so a
   queued document survives a restart and a failed one can be retried.
2. **Accept fast.** The upload request runs only the checks that need no
   parsing (type, size, PDF signature, UTF-8, empty text) and deduplicates by
   SHA-256, then stores and queues the document and returns `202`.
3. **Claim by compare-and-set.** A worker moves a document to `processing`
   with one `UPDATE … WHERE id = ? AND status = ? AND claim_token IS ?` and
   proceeds only if it changed one row. Each claim gets a fresh token.
4. **Leases.** A claim expires `RAG_INGEST_LEASE_S` (300 s) after its last
   renewal; workers renew before each batch of 64 chunks. Workers also claim
   `processing` documents whose lease has lapsed: that is how a crash is
   recovered. After `RAG_INGEST_MAX_ATTEMPTS` (3) interruptions the document
   fails with `interrupted`, so a file that kills the process can't loop.
5. **Fence the result.** Completion (status `ready`, chunks, upload deleted)
   is one transaction that applies only if the worker's token still matches. A
   worker that lost its claim, or whose document was deleted, discards its work.
6. **Failures are final until retried.** Errors mark the document `failed`
   with a code and a message for the uploader; unexpected exceptions record
   only their type. Nothing retries automatically. Retrying
   (`POST /v1/documents/{id}/retry`) or uploading the same bytes again
   re-queues it.
7. **Workers are threads in each API process** (`RAG_INGEST_WORKERS`,
   default 1). Uploads wake the local workers at once; other processes poll
   every `RAG_INGEST_POLL_INTERVAL_S`. On shutdown a worker hands its document
   back as `queued` at the next batch boundary, without counting the attempt.
   The CLI and the evaluation runner process in the calling thread.

Verified by tests for every state and transition, duplicate uploads in each
state, a lapsed lease taken over while the first worker finishes late (its
chunks are discarded, not duplicated), repeated interruptions, shutdown
hand-back, deletion mid-processing, restarts with queued and interrupted
documents, and two workers with separate database engines (as two processes
would have), two threads each, processing eight documents exactly once ([test_background_ingestion.py](../../tests/integration/test_background_ingestion.py),
[test_api.py](../../tests/integration/test_api.py)).

## Consequences

### Positive

- Uploads return in milliseconds; status, failure reason and attempts are
  visible per document and in `/ready`.
- No upload is lost to a restart, and a crashed worker's document is resumed
  without operator action.
- The claim, lease and fencing protocol maps directly onto PostgreSQL, where
  `SKIP LOCKED` can replace the compare-and-set loop.

### Negative and risks

- Raw uploads are now stored in the database until processing succeeds, and
  indefinitely for failed documents nobody retries or deletes. Security and
  operations docs say so.
- After a crash, the document waits for its lease to lapse (up to 5 minutes
  by default) before another worker resumes it.
- Parsing still runs in the API process. A hostile file can exhaust its memory
  or CPU, bounded only by the attempt limit.
- Polling costs one indexed query per process every 2 s when idle.
- Write throughput is still SQLite's single writer.

## Revisit when

- Untrusted users can upload: move parsing to a separate, resource-limited
  `rag worker` process. The claim protocol already supports it.
- Accounts arrive and storage moves to PostgreSQL (Phase 5): use
  `SKIP LOCKED` for claims, `LISTEN/NOTIFY` instead of polling, and object
  storage for uploads.
- Ingestion volume makes `database is locked` errors appear.
