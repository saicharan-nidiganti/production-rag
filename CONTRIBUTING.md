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

Use descriptive feature branches:

```text
feature/<short-description>
fix/<short-description>
docs/<short-description>
experiment/<short-description>
```

## Development Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras
uv run rag db upgrade
```

## Checks

Run these before opening a pull request. CI runs the same commands.

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python -m pytest --cov                                  # coverage floor: 85%
uv run rag eval --thresholds eval/thresholds.json              # retrieval regression gate
```

The test suite runs offline. Tests marked `live` call the Claude API or download an embedding
model, and run only with `RAG_RUN_LIVE_TESTS=1`.

## Changes That Affect Quality

- **Retrieval, chunking or embedding changes**: include `rag eval` results before and after in
  the pull request. If metrics improve, raise the thresholds in `eval/thresholds.json` in the
  same change. Never lower a threshold to make a change pass without explaining why in the pull
  request.
- **Dataset changes**: add a new version (`golden_v2.jsonl`) rather than editing an existing one,
  so earlier reports stay comparable.
- **Schema changes**: add an Alembic migration (`uv run python -m alembic revision -m "..."`).
  `tests/integration/test_database.py` fails if the models and migrations disagree.
- **Architectural decisions**: record them as an ADR in [docs/decisions/](docs/decisions/).
