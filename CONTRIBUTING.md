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

## Branches

Never commit directly to `main`. Use descriptive branches:

```text
feature/<short-description>
fix/<short-description>
docs/<short-description>
chore/<short-description>
experiment/<short-description>
```

`experiment/` branches are for retrieval or model experiments. They are
merged only when they come with an evaluation result that justifies the change.

## Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```text
<type>(<optional scope>): <imperative summary>

<body: what changed and why>
```

Types: `feat`, `fix`, `docs`, `chore`, `test`, `refactor`, `perf`, `ci`, `build`.
Scopes follow the system's components, for example `ingestion`, `retrieval`,
`generation`, `api`, `auth`, `frontend`, `eval`.

## Pull Requests

- Keep each pull request to one logical change.
- Fill in the pull request template, including how the change was tested.
- All CI checks must pass before merge.
- Changes to retrieval, chunking, prompts or models must include before/after
  evaluation results on the current golden dataset (see
  [docs/methodology.md](docs/methodology.md)).
- Significant technical decisions need an Architecture Decision Record (see below).

## Code Standards

The tooling below is introduced in Phase 1 (platform skeleton). Until it lands,
these are the conventions to follow.

### Python (backend, evaluation)

- Python 3.11+, managed with [uv](https://docs.astral.sh/uv/).
- Format and lint with Ruff. Type-check with mypy in strict mode.
- Type hints on all public functions. Pydantic models at API boundaries.
- No business logic in API route handlers. Keep presentation, business logic,
  retrieval, data access and infrastructure separate.

### TypeScript (frontend)

- `strict` TypeScript, ESLint, and formatting enforced in CI.
- API types are generated from the backend's OpenAPI schema, not hand-written.

## Testing Expectations

- Unit tests for pure logic (parsing, chunking, fusion, citation verification).
- Integration tests for database, API and ingestion flows against a real
  PostgreSQL instance.
- Every bug fix includes a regression test that fails without the fix.
- Tests must not call paid external APIs. Use the deterministic test providers.
- Never skip, weaken or delete a meaningful test to make CI pass.

## Architecture Decisions

Record significant decisions as ADRs in [docs/decisions/](docs/decisions/),
using the [template](docs/decisions/template.md). An ADR is needed when a change
adds a dependency with lasting impact, changes the data model or retrieval
strategy, or alters a security boundary.

## Security

- Never commit secrets. Local configuration goes in `.env`, which is git-ignored.
  Document every variable in `.env.example` with a placeholder value.
- Never commit real or private documents. Test fixtures must be synthetic or
  public-domain, with their source recorded.
- Do not log document contents, questions or personal information.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
