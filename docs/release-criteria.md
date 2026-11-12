# Release criteria

What a release must satisfy, how each criterion is checked, and which ones block it. The
[release workflow](../.github/workflows/release.yml) runs every check on the tagged commit
and records the result in a checklist ([`scripts/release_checklist.py`](../scripts/release_checklist.py)),
attached to the GitHub release. The rules decide; nobody judges a result by hand.

## States and rules

| State | Meaning |
| --- | --- |
| pass | The check ran and passed |
| fail | A blocking check failed, or didn't run |
| warn | A non-blocking check failed, or the performance gate warned |
| not measured | The criterion can't be checked for this release; the reason is recorded |
| not applicable | Another workflow enforces it |

- **Any fail stops the release.** The publish job doesn't run, and nothing is published.
- **A warning doesn't stop it,** and is shown in the checklist and the release notes.
- **A criterion that isn't measured** is named in the release notes as a limitation. Only
  the owner can accept one, for a stated reason.

## Criteria

Thresholds come from the project's own measurements, with the calibration recorded in each
gate file. They aren't round numbers chosen in advance.

| Id | Category | Criterion | Check and threshold | Blocking |
| --- | --- | --- | --- | --- |
| `version` | Reproducibility | The tag, `pyproject.toml` and the package agree on the version, and `CHANGELOG.md` has a dated section for it | String comparison | Yes |
| `lock` | Reproducibility | The lock file matches `pyproject.toml` | `uv lock --check` | Yes |
| `ci` | Reproducibility | CI passed on the tagged commit | The CI run of that commit: Python 3.12, 3.13 and 3.14, PostgreSQL, the web interface, the benchmark, markdown lint | Yes |
| `lint` | Functional | Lint and formatting | Ruff, no findings | Yes |
| `types` | Functional | Type checking | `mypy --strict`, no errors | Yes |
| `tests-sqlite` | Functional | The test suite on SQLite | Every test passes; coverage at least 90% | Yes |
| `tests-postgres` | Functional | The test suite on PostgreSQL with pgvector, including cross-user isolation | Every test passes | Yes |
| `web` | Functional | The web interface | ESLint with accessibility rules, `tsc`, every test, the production build | Yes |
| `smoke` | Functional | A real server works end to end | `scripts/smoke_test.py`: health, upload to ready, search, a cited answer, a stream, metrics, an online backup | Yes |
| `retrieval-offline` | Retrieval | Retrieval quality, offline configuration, SQLite | `eval/thresholds.json`: hit rate ≥ 0.93, recall ≥ 0.92, MRR ≥ 0.84, test-split hit rate ≥ 0.93, no false abstention | Yes |
| `retrieval-postgres` | Retrieval | The same, on PostgreSQL | `eval/thresholds.json` | Yes |
| `retrieval-recommended` | Retrieval | Retrieval quality, recommended configuration: bge-small embeddings and reranking | `eval/thresholds.reranked.json` | Yes |
| `citations` | Generation and citations | Answer assembly and citation mapping | `eval/thresholds.generation.json`, with the extractive generator: answered ≥ 0.98, citation precision ≥ 0.55, fact recall ≥ 0.83, no failed case | Yes |
| `claude` | Generation and citations | Claude's answers: declining unanswerable questions, resisting injection, citation quality | `eval/thresholds.claude.json`: correct declining ≥ 0.90, false abstention ≤ 0.10, 0 successful injections | No, while not measured |
| `performance` | Performance | Latency and throughput limits, and no regression | `eval/bench_thresholds.json`: retrieval p95 < 300 ms at 10,000 chunks, first token p95 < 3 s, a 100-page PDF in < 60 s, no HTTP errors; regressions against the CI baseline fail at 4× | Yes, for failures; warnings don't block |
| `audit` | Security | No known vulnerability in a dependency | `pip-audit --strict` on every locked Python package; `npm audit` on the packages served to browsers | Yes |
| `secrets` | Security | No secrets in the repository | gitleaks over the whole history | Yes |
| `codeql` | Security | No open static-analysis finding | Open CodeQL alerts: 0 | Yes |
| `image` | Deployment | The container image works and is free of fixable critical or high vulnerabilities | `container.yml` on the same tag builds, runs, checks and scans the image, and publishes it only if all pass | Enforced there |
| `package` | Deployment | The wheel and sdist build, and the wheel works on its own | `make package`: build, install the wheel in an empty environment, migrate, ingest, search | Yes |
| `reproducible` | Deployment | The build is reproducible | Two builds from the tag give byte-identical files | Yes |
| `docs` | Documentation | The documentation is consistent | markdownlint; the test suite checks every link and anchor, that every setting is documented, and the changelog | Yes |

## Covered by other checks

Some requirements of a release are met by checks above rather than by criteria of their
own:

- **Authentication, authorization and isolation:** the isolation suite in
  `tests/integration/test_isolation.py`, run on both databases.
- **Input limits and rate limiting:** `tests/integration/test_request_guard.py`.
- **Health and readiness:** the tests, the smoke test and the image's checks.
- **Configuration:** every setting is documented and present in `.env.example`, checked by
  `tests/unit/test_docs.py`; invalid settings stop startup, checked by
  `tests/unit/test_config_and_logging.py`.
- **Rollback:** the procedure is in [runbooks.md](runbooks.md#rollback) and
  [operations.md](operations.md#upgrading-and-rolling-back).
- **Documentation coverage:** [docs/README.md](README.md) indexes installation,
  configuration, API, architecture, evaluation, deployment, operations, troubleshooting,
  security and known limitations.

## What a release records

The release carries everything needed to reproduce it and judge it:

| Artifact | Contents |
| --- | --- |
| The wheel and sdist, `SHA256SUMS` | The package, and the checksums of its files |
| Build provenance | A signed attestation of the workflow run, commit and inputs that built the files: `gh attestation verify <file> -R saicharan-nidiganti/production-rag` |
| `checklist.md`, `checklist.json` | Every criterion and its state, the version and the commit |
| `eval-*/` | The evaluation reports: dataset and its SHA-256, corpus fingerprint, configuration, models, version and commit, every metric and every question |
| `benchmark/` | The benchmark report: environment, configuration, workload, every measurement and the gate's comparison with the baseline |
| `tests/` | JUnit results for both databases |
| `security/` | The pip-audit, npm audit and gitleaks reports |

The reports are attached as `release-reports.tar.gz`. The container image, its SBOM and its
provenance are on ghcr.io under the version's tag.

## Accepted limitations for 1.0.0

| Criterion | State | Reason | Accepted |
| --- | --- | --- | --- |
| `claude` | not measured | The Anthropic account has no API credit, so the paid evaluation can't run. Claude's answers are tested against a fake client and the real SDK's stream parser, not live | By the owner, 2026-11-12, on condition the release notes say so plainly |

## Running the checks before tagging

The workflow runs by hand as a dry run, which validates `main` and publishes nothing:

```bash
gh workflow run release.yml -R saicharan-nidiganti/production-rag
```

Locally, `make check`, `make smoke`, `make bench`, `make web` and `make package` run most of
the same checks.
