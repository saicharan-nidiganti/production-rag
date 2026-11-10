# Deployment requirements

What the backend needs to run outside a development machine, and the decisions behind how
it is deployed. The procedures are in [operations.md](operations.md); this page is the
requirements they implement.

## Deployment target

| Target | Fit | Decision |
| --- | --- | --- |
| One machine or VM running the container image | Matches the architecture: one process, one SQLite file, no other services | **The supported target** |
| One machine without containers (`rag serve` under systemd) | The same, with Python and `uv` managed by hand | Supported |
| A managed container service with a persistent volume (one instance) | Works if the volume is a real disk, not a network file share: SQLite needs working file locks | Supported, one instance only |
| Kubernetes | One replica with a `ReadWriteOnce` volume works; more replicas don't, since they can't share the database | Possible, with no advantage until PostgreSQL |
| Serverless functions | No persistent local disk; the models take seconds to load on a cold start | Not supported |
| Several instances behind a load balancer | Needs a shared database, queue and rate-limit store | Planned with PostgreSQL in Phase 5 ([roadmap](roadmap.md)) |

A single instance is a deliberate limit, not an oversight
([ADR-0002](decisions/0002-v0-storage-and-scope.md)). Measured on a laptop, one instance
answers about 11 questions per second with the recommended models and holds 100,000 chunks
comfortably ([performance.md](performance.md#capacity-planning)), which covers the intended
use: a team's own documents. Distributed infrastructure would add cost and failure modes
that this workload doesn't justify.

## Runtime requirements

| Resource | Minimum (offline configuration) | Recommended (bge-small, reranking, Claude) | Basis |
| --- | --- | --- | --- |
| CPU | 1 core, x86-64 | 4 or more cores | The embedding and reranking models use every core; the benchmark used 16 logical cores. The published image is x86-64 only; ARM64 is untested |
| Memory | 512 MiB | 4 GiB | Measured peak at 10,000 chunks: about 190 MiB offline, about 1.6 GiB with both models loaded |
| Disk | 1 GiB | 10 GiB on an SSD, encrypted | 5.5 KiB per chunk; about 150 MB of models; room for backups |
| GPU | None | None | The models run on the CPU through ONNX Runtime |
| Network in | The API port, behind a TLS proxy | The same | |
| Network out | None | HTTPS to `api.anthropic.com`; Hugging Face once, to download the models | The offline configuration makes no network calls |
| Operating system | Linux for the image; Linux or Windows from source | Linux | CI runs on Ubuntu; parser memory limits need Linux or macOS |
| Runtime | Python 3.12 or later, or a container runtime | The container image | CI tests 3.12, 3.13 and 3.14 |
| Model access | None | An Anthropic API key with credit | |

Sizing beyond these numbers is in [operations.md](operations.md#sizing).

## Packaging

- **A wheel and a container image** per release, both built from `uv.lock`, so the
  dependency set is the one that was tested. CI fails if the lock is out of date.
- **The image** runs as an unprivileged user, holds no secrets, takes all configuration from
  the environment, keeps its state in `/data`, has a health check, and applies migrations
  when it starts. It is built, run, checked and scanned before it is published.
- **No assets to build**: there is no front end. The models are downloaded on first use, or
  pre-downloaded into `/data/models` for an offline host.

## Environments

The code and image are identical in every environment. Only the environment variables
differ ([configuration.md](configuration.md)).

| Environment | Database | Embeddings and reranking | Generator | API key | Purpose |
| --- | --- | --- | --- | --- | --- |
| Development | `data/rag.db` | Hashing, none | Extractive | None | Fast, offline, free |
| Test (CI) | Temporary, per test | Hashing, none | Extractive, or a fake client | A made-up key where needed | Deterministic; no network |
| Evaluation | Temporary, per run | Each configuration under test | Extractive; Claude only with `--allow-paid` | None | Quality and performance gates |
| Staging | Its own volume | As production | Claude, with a low rate limit | Its own key | Rehearse upgrades and restores |
| Production | An encrypted volume, backed up | fastembed, reranking on | Claude | A random key | Real use |

Staging and production never share a database, an API key or an Anthropic key.

## Deployment process

Each step must pass before the next. A release never skips the gates.

| Step | What runs | Where |
| --- | --- | --- |
| 1. Build | The wheel and sdist from a clean checkout; the image from `uv.lock` | CI `package`; the container workflow |
| 2. Validate | Lock check, lint, `mypy --strict`, Markdown lint, CodeQL, secret scan, dependency audit | CI |
| 3. Test | The offline suite, with a 90% coverage floor, on three Python versions | CI `quality` |
| 4. Gate | The retrieval regression gate; the performance gate | `make eval-gate`, `make bench` |
| 5. Package | The wheel installed alone runs the CLI; the image becomes ready, ingests, answers with citations, streams, serves metrics, backs up, runs as non-root, and passes the vulnerability scan | CI `package`; the container workflow |
| 6. Deploy | Back up, install the new version, migrate, restart | [operations.md](operations.md#upgrading-and-rolling-back) |
| 7. Verify | `/ready` at the expected schema revision, one question, one upload | [runbooks.md](runbooks.md#verifying-recovery) |
| 8. Roll back | The previous version; with the pre-upgrade backup if a migration ran | [runbooks.md](runbooks.md#rollback) |

The release checklist in [release.md](release.md) covers steps 1 to 5 for a tag.

## Reliability

| Requirement | How it is met |
| --- | --- |
| Liveness and readiness | `/health` checks nothing; `/ready` checks the database and the schema revision. The image's health check uses `/health` |
| Startup failures | Invalid settings stop startup with one line per variable and never print a value. A schema behind the code leaves `/ready` at 503 and starts no workers |
| Dependency failures | The database: `/ready` 503 and `rag_database_up 0`. Claude: 503 for transient failures, 502 for configuration, never a hang. The offline generator has no dependency |
| Timeouts | Claude calls: `RAG_ANTHROPIC_TIMEOUT_S` (120 s) with the SDK's retries. Parsing: `RAG_PARSE_TIMEOUT_S`, after which the parser process is killed. SQLite waits up to 5 s for a lock |
| Graceful shutdown | On SIGTERM the server stops accepting requests, and each worker hands its document back to the queue without counting the attempt |
| Restarts | Safe at any time: the queue is in the database, so queued work survives. A document whose worker died is taken over after its lease. Use `restart: unless-stopped` or the orchestrator's equivalent |
| Resource exhaustion | Upload, request-body and PDF-page limits; parser memory limit; per-client rate limits. A full disk fails writes without corrupting committed data |
| Backup and recovery | `rag db backup`, `verify` and `restore` ([data-lifecycle.md](data-lifecycle.md#recovery)) |

## Scaling

| Need | Now | Later |
| --- | --- | --- |
| More questions | A larger machine; the models scale with cores | Several instances on PostgreSQL |
| More documents | Up to about 100,000 chunks per process | pgvector with an approximate index |
| Faster ingestion | A larger machine; ingestion already runs in the background on a queue | Workers on other machines |
| Caching | The vector matrix is cached per process and reloads when the index changes. Answers aren't cached: they depend on the index and the model | |
| Load balancing | Not applicable with one instance; the reverse proxy terminates TLS | With several instances |

## Cost controls

| Driver | Control |
| --- | --- |
| Claude tokens | Abstention answers without a model call when retrieval finds no evidence. `RAG_TOP_K`, `RAG_CHUNK_SIZE` and `RAG_ANTHROPIC_MAX_TOKENS` bound a call's size. `RAG_RATE_LIMIT_QUERY_PER_MINUTE` bounds calls per client. `rag_generation_tokens_total` shows spend |
| Development and CI | The default configuration is offline and free. Paid runs need `--allow-paid`, and `rag eval --plan` shows the cost first |
| Embeddings | Local models: CPU time only, no per-call cost |
| Compute | One small machine; no GPU |
| Storage | 5.5 KiB per chunk; retention for failed uploads and backups |
| Observability | Logs are one or two lines per request; metric labels are bounded |

## Security

Secrets come from the environment or a secret store, never from the image or the
repository. Only the proxy is exposed, with TLS. The API key guards every route but the
probes. The image is scanned before it is published. The operator checklist and the audit
are in [security.md](security.md).

## Observability

Deployed instances expose JSON logs on stderr, Prometheus metrics at `/metrics`, and health
at `/health` and `/ready`. Tracing is deferred while a request stays in one process.
Evaluation and benchmark results are CI artifacts and committed reference reports, not
runtime signals ([observability.md](observability.md)).

## Disaster recovery

| What | Requirement | How |
| --- | --- | --- |
| The database | Daily backups, copied off the host encrypted; a restore tested monthly | `rag db backup --keep 7`; [data-lifecycle.md](data-lifecycle.md#backup) |
| Configuration | Reproducible from configuration management; secrets from a secret store | Not in the database or its backups |
| The service | Rebuildable from the image tag and the configuration alone | A new host needs only the image, the environment and a restored `/data` |
| Loss tolerated | Up to one backup interval of uploads, which can be uploaded again | |
| Time to recover | Minutes: pull the image, restore, start | [runbooks.md](runbooks.md#the-database-is-damaged) |
