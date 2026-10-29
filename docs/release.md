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
| 3 | The version is the same in `pyproject.toml`, `production_rag.__version__` and the newest changelog section | `tests/unit/test_docs.py` |
| 4 | The wheel installs on its own and its CLI migrates, ingests and searches | `make package` (CI job) |
| 5 | Migrations upgrade an empty database and the previous release's schema, and downgrade | `tests/integration/test_database.py` |
| 6 | No known vulnerabilities in the locked dependencies; no secrets in the history | pip-audit and gitleaks (CI security job) |
| 7 | Evaluation gates pass, and the reference reports behind published numbers are committed | `make eval-gate`, the free live gates, [eval/results/](../eval/results/) |
| 8 | The docs match the code: README status table, [status.md](status.md), known limitations, configuration (tested) | Read through; `tests/unit/test_docs.py` for links, settings and diagrams |
| 9 | Live-model behaviour is either verified or stated as unverified in the release notes | [evaluation.md](evaluation.md#paid-evaluation-with-claude) |
| 10 | CHANGELOG: `[Unreleased]` becomes `[X.Y.Z] - date`, with a new empty `[Unreleased]` above it | Edit, commit, wait for CI |
| 11 | Tag and release | `git tag -a vX.Y.Z -m "..." <commit> && git push origin vX.Y.Z`, then create the GitHub release from the tag with the notes; attach the wheel and sdist from `uv build` |

## v0.1.0: status

The release commit is the one that adds the `[0.1.0]` section to the changelog and
these notes. A commit can't record its own CI results, so the checklist results for it go
in the report to the owner and in the GitHub release, not in this file.

- Last complete run before the release commit: `04e33d7`, every item passed.
- Item 9: the owner chose to release with the limitations stated, as below, rather than
  wait for the paid Claude evaluation.
- Items 10 and 11: the changelog is ready; the tag and the GitHub release wait for the
  owner's approval of the final verification.

## v0.1.0 release notes

To be published with the `v0.1.0` tag.

> **Production RAG v0.1.0: the first release of the v0 backend.**
>
> A self-hosted retrieval-augmented question-answering backend: upload
> documents, ask questions, get answers that cite the exact passages they used,
> or a clear "the documents don't say" when retrieval finds nothing usable.
>
> **What's in it**
>
> - Background ingestion of TXT, Markdown and PDF, with a status per document
>   (`queued`, `processing`, `ready`, `failed` with a reason), retries and
>   recovery after crashes and restarts. Each upload is parsed in a separate
>   process with a timeout and no secrets in its environment.
> - Hybrid retrieval (BM25 and `bge-small` embeddings fused with Reciprocal Rank
>   Fusion) with optional cross-encoder reranking.
> - Abstention before generation when retrieval finds no usable evidence.
> - Answers from Claude with native citations mapped to stored passages, or an
>   offline extractive mode that needs no API key.
> - **Stable chunk keys.** Every chunk has a `chunk_key`, a hash of the
>   document's SHA-256 and the chunk's character span. Re-ingesting identical
>   bytes with the same `RAG_CHUNK_SIZE` and `RAG_CHUNK_OVERLAP` gives the same
>   keys, even after the document was deleted and its row ids changed. Changed
>   content always gets new keys, and changing the chunk settings changes the
>   key of every chunk whose span moves. `chunk_id` is a database row id: it may
>   change, and SQLite may reuse it.
> - A FastAPI HTTP API with optional API-key authentication, and the `rag` CLI.
> - **Evaluation** on 94 questions with a held-out test split, regression gates
>   in CI and a cost preview for paid runs. `rag eval` prints a readable summary
>   (configuration, commit, every metric for all questions and per split,
>   latency, billed tokens, failed cases); `--json` gives the machine-readable
>   form. A generation error on one question is recorded on that question,
>   with its message and whether it's retryable, instead of aborting the run.
>   Rates cover the completed questions, and the Claude acceptance gate requires
>   zero failed ones.
> - **Configuration that fails safely.** Invalid settings stop startup with one
>   line per problem naming the variable. The rejected value is never printed,
>   so a mistyped API key can't end up in a terminal or CI log; `rag` exits
>   with code 2.
>
> **Measured** (held-out split, offline; raw reports in
> `eval/results/2026-10-29/`): hit rate 1.000 and MRR 0.946 with reranking; all
> 4 off-topic questions declined before generation; no false abstentions. The
> dataset is small and was written for this project, so treat these as a
> regression baseline, not a benchmark.
>
> **Not verified live**: Claude's answer quality and citation precision; its
> declining of on-topic questions the documents can't answer (retrieval
> catches none of the 8 held-out ones, so this depends entirely on the model);
> and its resistance to the planted prompt injection. All three are
> implemented and tested offline against a fake client; the paid evaluation
> hasn't run, and the Claude API has not yet accepted a request from this code.
>
> **Known limitations**: single tenant (one API key, no accounts); SQLite on
> one machine; parsing is isolated but not sandboxed (it can reach the network
> and filesystem, and has no memory limit on Windows), so restrict uploads to
> trusted users; no rate limiting; no DOCX or HTML; no answer streaming; macOS
> is untested.
>
> **Installing**: `uv sync --all-extras && uv run rag db upgrade`, which
> applies migrations 0001 to 0003 (see the README). Full list of changes:
> CHANGELOG.md.
