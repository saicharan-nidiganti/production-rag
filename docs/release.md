# Releasing

How a version is cut, and the state of the next one. A release is a signed-off
commit on `main` with an annotated tag `vX.Y.Z` and a GitHub release whose notes
come from [CHANGELOG.md](../CHANGELOG.md). The maintainer creates the tag and
the release; nothing here is automated yet.

## Checklist

Run against the exact commit to be tagged.

| # | Check | How |
| --- | --- | --- |
| 1 | CI passes on the commit: quality on Python 3.12, 3.13 and 3.14, package, security, docs | GitHub Actions run for that commit |
| 2 | A fresh clone runs the README Quickstart and API examples, and passes the test suite, the retrieval gate and the smoke test | `git clone`, then the README commands, `make check`, `make smoke` |
| 3 | The version is the same in `pyproject.toml` and `production_rag.__version__` | `tests/unit/test_docs.py` |
| 4 | The wheel installs on its own and its CLI migrates, ingests and searches | `make package` (CI job) |
| 5 | Migrations upgrade an empty database and the previous release's schema, and downgrade | `tests/integration/test_database.py` |
| 6 | No known vulnerabilities in the locked dependencies; no secrets in the history | pip-audit and gitleaks (CI security job) |
| 7 | Evaluation gates pass, and the reference reports behind published numbers are committed | `make eval-gate`, the free live gates, [eval/results/](../eval/results/) |
| 8 | The docs match the code: README status table, [status.md](status.md), known limitations, configuration (tested) | Read through; `tests/unit/test_docs.py` for links, settings and diagrams |
| 9 | Live-model behaviour is either verified or stated as unverified in the release notes | [evaluation.md](evaluation.md#paid-evaluation-with-claude) |
| 10 | CHANGELOG: `[Unreleased]` becomes `[X.Y.Z] - date`, with a new empty `[Unreleased]` above it | Edit, commit, wait for CI |
| 11 | Tag and release | `git tag -a vX.Y.Z -m "..." <commit> && git push origin vX.Y.Z`, then create the GitHub release from the tag with the notes; attach the wheel and sdist from `uv build` |

## v0.1.0: status

Checked on 2026-10-29 against commit `00623f9`.

| # | Result |
| --- | --- |
| 1 | Pass: all six jobs on `00623f9` |
| 2 | Pass on Windows: fresh clone, Quickstart, the README's `curl` examples against `rag serve`, 324 tests, the gate and the smoke test, with no `RAG_*` or `ANTHROPIC_*` variables set. On Linux, CI covers the same steps except the README walkthrough |
| 3 | Pass: `0.1.0` in both places |
| 4 | Pass: CI package job; the wheel contains both migrations and no tests |
| 5 | Pass |
| 6 | Pass on `00623f9` (pip-audit is re-run weekly) |
| 7 | Pass: CI gate and the free live gates; three reference reports committed |
| 8 | Pass, after this session's fixes (two architecture diagrams didn't render; README results now point at committed reports) |
| 9 | **Decision needed.** Claude's answers are implemented but not measured. Either run the focused paid evaluation first, or release with the limitation stated, as the draft notes below do |
| 10 | Not done: waits for the go-ahead |
| 11 | Not done: waits for the go-ahead |

## v0.1.0 release notes (draft)

> **Production RAG v0.1.0: the first release of the v0 backend.**
>
> A self-hosted retrieval-augmented question-answering backend: upload
> documents, ask questions, get answers that cite the exact passages they used,
> or a clear "the documents don't say" when they don't.
>
> **What's in it**
>
> - Background ingestion of TXT, Markdown and PDF, with a status per document
>   (`queued`, `processing`, `ready`, `failed` with a reason), retries and
>   recovery after crashes and restarts.
> - Hybrid retrieval (BM25 and `bge-small` embeddings fused with Reciprocal Rank
>   Fusion) with optional cross-encoder reranking.
> - Abstention before generation when retrieval finds no usable evidence.
> - Answers from Claude with native citations mapped to stored passages, or an
>   offline extractive mode that needs no API key.
> - A FastAPI HTTP API with optional API-key authentication, and the `rag` CLI.
> - An evaluation suite (94 questions, held-out test split) with regression gates
>   in CI, and a cost preview for paid runs.
>
> **Measured** (held-out split, offline, see `eval/results/2026-10-29/`): hit rate
> 1.000 and MRR 0.946 with reranking; all 4 off-topic questions declined before
> generation; no false abstentions. The dataset is small and was written for this
> project, so treat these as a regression baseline, not a benchmark.
>
> **Not yet verified**: Claude's answer quality, its declining of on-topic
> questions the documents can't answer, and its resistance to prompt injection.
> They are implemented and tested offline; the live evaluation hasn't run.
>
> **Known limitations**: single tenant (one API key, no accounts); SQLite on one
> machine; uploads are parsed in an isolated process with a timeout and (Linux,
> macOS) a memory limit, but not sandboxed, so restrict uploads to trusted
> users; no rate limiting; no DOCX or HTML yet.
>
> **Install**: `uv sync --all-extras && uv run rag db upgrade` (see the README).
> Full list of changes: CHANGELOG.md.
