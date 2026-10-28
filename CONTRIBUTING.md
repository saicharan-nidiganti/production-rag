# Contributing

Thank you for contributing to Production RAG.

## Development Principles

Contributions should prioritize:

- Correctness
- Reproducibility
- Testability
- Maintainability
- Clear documentation
- Measurable improvements

## Before Making Changes

1. Understand the relevant issue.
2. Review the existing documentation.
3. Avoid unnecessary architectural changes.
4. Add or update tests where appropriate.
5. Update documentation when behavior changes.

## Development Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras    # dependencies from uv.lock, including local embeddings
uv run rag db upgrade   # create the local SQLite database (data/rag.db)
```

The README's [Quickstart](README.md#quickstart) covers running the API and
asking questions.

## Checks

Run these before pushing. CI runs the same commands.

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python -m pytest --cov                       # coverage floor: 85%
uv run rag eval --thresholds eval/thresholds.json   # retrieval regression gate
```

The test suite runs offline. Tests marked `live` call the Claude API or
download an embedding model. They run only with `RAG_RUN_LIVE_TESTS=1`.

On Windows, use `uv run python -m pytest` rather than `uv run pytest`: some
Application Control policies block the `pytest.exe` launcher.

## Branches and Commits

The maintainer, including automated sessions working on the maintainer's
behalf, commits verified changes directly to `main`. Every push runs CI, and a
change that breaks CI is fixed or reverted immediately. Force-pushes and
history rewrites on `main` are not allowed.

Other contributors use a branch and a pull request:

```text
feature/<short-description>
fix/<short-description>
docs/<short-description>
chore/<short-description>
experiment/<short-description>
```

`experiment/` branches are for retrieval or model experiments. They are
merged only when they come with an evaluation result that justifies the change.

Use [Conventional Commits](https://www.conventionalcommits.org/):

```text
<type>(<optional scope>): <imperative summary>

<body: what changed and why>
```

Types: `feat`, `fix`, `docs`, `chore`, `test`, `refactor`, `perf`, `ci`, `build`.
Scopes follow the system's components, for example `ingestion`, `retrieval`,
`generation`, `api`, `db`, `eval`, `auth`, `frontend`.

## Pull Requests

- Keep each pull request to one logical change.
- Fill in the pull request template, including how the change was tested.
- All CI checks must pass before merge.
- Changes to retrieval, chunking, prompts or models must include before/after
  evaluation results on the current golden dataset (see
  [docs/evaluation.md](docs/evaluation.md)).
- Significant technical decisions need an Architecture Decision Record (see below).

## Code Standards

### Python (backend, evaluation)

- Python 3.12+, managed with [uv](https://docs.astral.sh/uv/) and a committed
  `uv.lock`.
- Format and lint with Ruff. Type-check with mypy in strict mode.
- Type hints on all public functions. Pydantic models at API boundaries.
- No business logic in API route handlers. Keep presentation, business logic,
  retrieval, data access and infrastructure separate. The module boundaries are
  described in [docs/architecture.md](docs/architecture.md#3-code-layout).

### TypeScript (frontend, from Phase 6)

- `strict` TypeScript, ESLint, and formatting enforced in CI.
- API types are generated from the backend's OpenAPI schema, not hand-written.

## Testing Expectations

- Unit tests for pure logic (parsing, chunking, fusion, metrics, citation
  mapping).
- Integration tests for the database, API, CLI and ingestion flows against a
  real SQLite database in a temporary directory.
- Every bug fix includes a regression test that fails without the fix.
- Tests must not call paid external APIs. Use the extractive generator or a
  fake client, and mark anything else `live`.
- Never skip, weaken or delete a meaningful test to make CI pass.

## Changes That Affect Quality

- **Retrieval, chunking or embedding changes:** include `rag eval` results
  before and after. If metrics improve, raise the thresholds in
  `eval/thresholds.json` in the same change. Never lower a threshold without
  explaining why.
- **Dataset changes:** add a new version (`golden_v2.jsonl`) instead of editing
  an existing one, so earlier reports stay comparable.
- **Schema changes:** add an Alembic migration
  (`uv run python -m alembic revision -m "..."`). `tests/integration/test_database.py`
  fails if the models and migrations disagree.

## Architecture Decisions

Record significant decisions as ADRs in [docs/decisions/](docs/decisions/),
using the [template](docs/decisions/template.md). An ADR is needed when a change
adds a dependency with lasting impact, changes the data model or retrieval
strategy, or alters a security boundary.

## Security

- Never commit secrets. Local configuration goes in `.env`, which is git-ignored.
  Document every variable in `.env.example` with a placeholder value.
- Never commit real or private documents. Test fixtures and evaluation corpora
  must be synthetic or public-domain, with their source recorded.
- Do not log document contents, questions or personal information.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
