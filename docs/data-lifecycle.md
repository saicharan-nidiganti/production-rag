# Data lifecycle

What the backend stores, where and for how long. It also covers how each piece can be
rebuilt, and how to back up, verify, restore and delete it. The commands are `rag db backup`,
`verify`, `restore`, `prune` and `rebuild-search-index`
([operations.md](operations.md#backups) has the day-to-day procedure).

## Data inventory

Everything the service writes lives in one SQLite file (ADR-0002), except the downloaded
models, logs and metrics.

| Data | Where | Class | Kept | Rebuilt from | In a backup |
| --- | --- | --- | --- | --- | --- |
| Uploaded file bytes | `document_files` | Sensitive | While the document is `queued`, `processing` or `failed`; deleted in the transaction that marks it `ready` | The operator's original | Yes |
| Extracted text | `documents.text` | Sensitive | As long as the document | Re-uploading the original | Yes |
| Document record: file name, title, type, size, content hash, status, error, timestamps, `metadata` | `documents` | Sensitive: file names can carry personal information | As long as the document | Re-uploading the original | Yes |
| Chunks: text, offsets, heading path, page | `chunks` | Sensitive | As long as the document | Re-uploading the original; chunking needs the section structure, which isn't stored | Yes |
| Embeddings | `chunks.embedding`, with `embedding_model` | Sensitive, derived | As long as the chunk | `rag reindex`, from the chunk text, deterministic for a given model | Yes: re-embedding takes about 50 seconds per thousand chunks with bge-small on a laptop CPU |
| Full-text index | `chunks_fts` | Derived | As long as the chunks | `rag db rebuild-search-index` | Yes |
| Vector matrix | Each process's memory | Derived | Until the index generation changes | Reloaded automatically | No |
| Embedding and reranking models | `RAG_FASTEMBED_CACHE_DIR` (`/data/models` in the image) | Public | Until removed | Downloaded again on first use | No |
| Configuration | Environment variables, `.env` | Internal; API keys are Secret | Managed by the operator | Configuration management | No: secrets belong in a secret store |
| Logs | JSON on stderr, kept by the log platform | Internal, no content ([observability.md](observability.md#sensitive-data)) | The platform's policy; 30 days is a sensible default | | No |
| Metrics | Process memory, kept by Prometheus | Internal | The platform's policy | | No |
| Evaluation corpus and golden datasets | `eval/` in git | Public, synthetic | Versioned in git | | Git |
| Evaluation reports | `eval/results/<date>/` committed; `eval/reports/` local, git-ignored | Public | Committed reports are kept; local ones are overwritten | `rag eval` | Git |
| Backups and their manifests | `backups/` beside the database, or `--output` | Sensitive: a full copy | `rag db backup --keep N`; off-host copies by your policy | | |
| Pre-restore copies | Beside the database | Sensitive | Until the operator deletes them | | |
| Temporary files | An upload over 1 MB is spooled to a temporary file by the web framework while it arrives. The parser process writes none: it reads the upload on stdin and answers on stdout | Sensitive | Until the request ends | | No |

## Classification

| Class | Examples | Handling |
| --- | --- | --- |
| Secret | `RAG_API_KEY`, `ANTHROPIC_API_KEY` | Environment or a secret store only. Never in the database, a backup, a log line or the image. Held as `SecretStr` and redacted from logs as a backstop |
| Sensitive | Uploaded files, extracted text, chunks, embeddings, file names, backups | Stored only in the database and its backups. Never logged. Sent outside only to the configured generator, as retrieved passages. Encrypt at rest and in backups |
| Internal | Logs, metrics, configuration other than secrets, document ids and content hashes | Operational access only. Contains no document content |
| Public | The evaluation corpus and datasets (synthetic), evaluation reports, models | No restriction |

## Storage

- **One SQLite file in WAL mode** holds documents, uploads, chunks, embeddings, the
  full-text index and the queue. A document's chunks are written in the transaction that
  marks it `ready`, so a crash leaves all of them or none.
- **Size:** about 5.3 KiB per chunk with bge-small embeddings, or 7.5 times the extracted
  text, measured on 2,743 chunks ([operations.md](operations.md#sizing)). Failed documents
  also keep their uploaded bytes until they are retried, deleted or pruned.
- **Access:** one writer at a time (ingestion and deletions), many readers (queries). Each
  process holds the vector matrix in memory and reloads it when the index generation changes.
- **Interfaces:** `DocumentRepository` (document queries), `IngestionQueue` (state changes
  under the claim protocol) and `Retriever` (search) are the only code that touches the
  tables. `db/maintenance.py` works on the file itself. Moving to PostgreSQL replaces those,
  with `pg_dump` and `pg_restore` in place of the backup commands
  ([ADR-0002](decisions/0002-v0-storage-and-scope.md)).

## Lineage

Every piece of evidence in an answer can be traced to the bytes it came from:

| Link | Recorded as |
| --- | --- |
| Citation → chunk | `chunk_id`, and `chunk_key`, which stays the same when identical content is re-ingested |
| Chunk → document | `document_id` |
| Chunk → text | `start_char`, `end_char` into the document's extracted text; `heading` and `page` |
| Document → source version | `content_hash`: the SHA-256 of the uploaded bytes. `chunk_key` is derived from it and the chunk's span |
| Chunk → embedding model | `embedding_model` on every chunk |
| Document → processing configuration | `metadata.ingested_with`: the version, `chunk_size` and `chunk_overlap` |
| Evaluation case → evidence | A source file and heading, not chunk ids, so cases survive re-chunking |
| Evaluation result → inputs | Each report records the dataset, configuration, models and the version |
| Backup → database | The manifest: schema revision, version, counts and the file's SHA-256 |

## Versioning

- **Source documents** are identified by content. Identical bytes are deduplicated into
  one document, while changed bytes become a new document. Nothing is replaced silently: the
  older version stays searchable until it is deleted. To update a document, upload the new
  version, wait until it is `ready`, then delete the old one.
- **Processing configuration** applies when a document is processed, and is recorded in
  `metadata.ingested_with`. Changing `RAG_CHUNK_SIZE` or `RAG_CHUNK_OVERLAP` affects new
  uploads only. To re-chunk a document, delete it and upload it again.
- **Embedding models** are recorded per chunk. Chunks embedded by another model are counted
  as `stale_chunks` in `/ready` and as `rag_stale_chunks` in the metrics. `rag reindex`
  re-embeds them, and vector search uses only the configured model's vectors.
- **The schema** is versioned by Alembic revisions, which `/ready`, `rag db current`,
  `rag db verify` and every backup manifest report.
- **Evaluation datasets** are versioned by file name (`golden_v1.jsonl`, `golden_v2.jsonl`)
  and in git. Reference results are kept in dated directories.

## Retention

| Data | Default | How to change it |
| --- | --- | --- |
| Ready documents and their chunks | Until deleted | `DELETE /v1/documents/{id}` |
| Uploaded bytes of ready documents | Deleted on completion | |
| Failed documents and their uploads | Until retried or deleted | `rag db prune --failed-older-than DAYS`, for example daily with 30 |
| Backups | All kept | `rag db backup --keep N` keeps the newest N in that directory |
| Pre-restore copies | Kept | Delete them once the restored service is confirmed |
| Logs and metrics | The platform's policy | The log platform and Prometheus |
| Local evaluation reports | Overwritten by the next run | |

Pruning never touches `ready`, `queued` or `processing` documents, or files that don't
follow the backup naming. Run it with `--dry-run` first to see what it would delete.

## Deletion

- **`DELETE /v1/documents/{id}`** removes the document, its chunks, their full-text entries
  and its stored upload, and moves the index generation forward so every process drops the
  vectors from its cache. A second request answers 404 and leaves the same state. Deleting a
  document that is being processed is safe: the worker finds it gone and discards its work.
- **Nothing is orphaned.** Chunks and uploads reference their document with
  `ON DELETE CASCADE`, and triggers keep the full-text index in step. `rag db verify`
  checks both.
- **Deleted content can persist.** SQLite marks deleted pages free without overwriting
  them, and backups keep what they captured. When content must be erased, for example
  personal data, delete the documents, stop the service, run
  `python -c "import sqlite3; sqlite3.connect('rag.db').execute('VACUUM')"` to rewrite the
  file, and let backups that contain it expire, or delete them.

## Backup

The database is backed up as one file, because almost all of it is either impossible to
rebuild without the operator's originals, or slow to rebuild:

- **Backed up:** extracted text, document records and their states, chunks with their
  offsets, embeddings, the full-text index and the queue.
- **Not backed up:** models (downloaded again), logs and metrics (the platforms keep them),
  configuration and secrets (configuration management and a secret store), and evaluation
  data (git).

`rag db backup` copies the database online with SQLite's backup API, including what is still
in the write-ahead log, while the service runs. It checks the copy with SQLite's integrity
check before giving it its final name, and writes a manifest beside it:

```json
{
  "format": 1,
  "created_at": "2026-11-10T04:39:20.504211+00:00",
  "app_version": "0.2.0",
  "sqlite_version": "3.50.4",
  "source": "rag.db",
  "schema_revision": "0003",
  "documents": {"queued": 0, "processing": 0, "ready": 2, "failed": 0},
  "chunks": 10,
  "embedding_models": {"hashing-v1-d512": 10},
  "size_bytes": 122880,
  "sha256": "4c659e937ab67af5…"
}
```

`rag db verify` checks the live database, or a backup with `--file`. For a backup it first
compares the file with the manifest's SHA-256, then runs these checks, none of which write
to the file:

| Check | Fails when |
| --- | --- |
| `checksum` | The backup changed after it was made |
| `sqlite_integrity` | SQLite's own integrity check finds damage, or the file isn't a database |
| `foreign_keys` | A chunk or upload references a document that doesn't exist |
| `schema` | The file isn't a migrated database, or its revision is newer than this version. An older revision passes, with a reminder to run `rag db upgrade` |
| `search_index` | The full-text index doesn't match the chunks |
| `ready_documents` | A ready document's chunk count is wrong, or it has no chunks |
| `partial_chunks` | A document that isn't ready has chunks |
| `uploads` | An unfinished document lost its upload, or a ready one still holds it |
| `embeddings` | One embedding model has vectors of different sizes |
| `index_state` | The index generation row is missing |

A suggested schedule is a daily `rag db backup --keep 7`, then copying the newest backup and
its manifest off the host, encrypted. Once a month, verify an off-host copy and restore it to
a scratch path, to know that restores work.

## Recovery

`rag db restore BACKUP --yes` verifies the backup and refuses one that fails. It then keeps
a copy of the database it replaces (`rag.pre-restore-<time>.db`), copies the backup in and
moves the index generation past both the old and the restored value, so a process that
still caches vectors reloads them. It ends by verifying the result. Stop the service first.

| Scenario | Procedure |
| --- | --- |
| The volume or disk is lost | On new storage: `rag db restore <off-host backup> --yes`, then `rag db upgrade` if it reports `needs_upgrade`, then start and check `/ready`. Re-upload documents ingested after the backup |
| `search_index` fails verification | `rag db rebuild-search-index` rebuilds it from the chunks; nothing else is lost |
| `sqlite_integrity` fails | Stop the service and restore the newest backup that verifies |
| Stale embeddings after a model change | `rag reindex` |
| A document was deleted by mistake | Upload the original again. Without it, restore a backup to a scratch path with `RAG_DATABASE_URL` pointing there, and recover the text from that copy |
| A migration failed | Restore the backup taken before the upgrade and run the previous version; there is no downgrade command ([operations.md](operations.md#upgrading-and-rolling-back)) |
| A deployment is rolled back | The same, if the new version applied a migration; otherwise only the code changes |

With daily backups, up to a day of changes can be lost. A restore takes about as long as
copying the file twice: once for the pre-restore copy and once for the backup.

## Privacy and security

- **At rest:** the application doesn't encrypt the database. Put `/data` on an encrypted
  volume, such as LUKS or your cloud provider's volume encryption, and encrypt backups
  before they leave the host, with `age`, `gpg` or the storage service's encryption.
- **In transit:** TLS at the reverse proxy. The only outbound traffic is HTTPS to the Claude
  API, with `RAG_GENERATOR=anthropic`.
- **Access:** the API key guards the API. On disk, the container runs as uid 10001; keep the
  database, backups and pre-restore copies readable by the service user only.
- **Data sent to a third party:** with the Claude generator, questions and retrieved passages
  go to Anthropic. Uploaded files and whole documents never do.
- **Personal information:** v0 has no user accounts. File names and document contents may
  contain personal information; they are never logged, and deletion is covered above.
- **No unnecessary copies:** uploads are deleted once processed. Large uploads pass
  through a temporary file only while they are received, so keep the temporary directory
  private to the service user. Parsing writes no files. Backups and pre-restore copies are
  the only other copies, so manage their retention.

## Growth

| Driver | Estimate |
| --- | --- |
| Documents | About 7.5 times their extracted text with bge-small embeddings: 5.3 KiB per chunk of about 840 characters |
| Failed uploads | Their full size, until pruned |
| Superseded versions | Their full size, until the old version is deleted |
| Logs | About 0.7 KB per question offline (`http.request` and `query.completed`), about 1 KB with Claude (`generation.completed` too), and about 0.75 KB per ingested document |
| Backups | N times the database with `--keep N`, plus off-host copies |
| Evaluation history | About 1.4 MB per committed set of reference reports |
