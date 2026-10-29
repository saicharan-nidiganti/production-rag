# Development tasks. CI runs these same targets, so a green `make check` locally means
# the quality job should pass. Each recipe is a plain `uv` command; without make (for
# example on Windows), run the command shown in the recipe. CONTRIBUTING.md lists them.
#
# Paid Claude runs are deliberately not a target: they need --allow-paid and the
# maintainer's approval (docs/evaluation.md#paid-evaluation-with-claude).

UV ?= uv
.DEFAULT_GOAL := help

.PHONY: help install lock-check lint format typecheck test eval-gate smoke package check 	live-test docs-lint serve clean

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-11s %s
", $$1, $$2}'

install: ## Install the locked dependencies, all extras and the dev tools
	$(UV) sync --frozen --all-extras

lock-check: ## Fail if uv.lock is out of date with pyproject.toml
	$(UV) lock --check

lint: ## Ruff lint and format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format: ## Apply Ruff's fixes and formatting
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

typecheck: ## mypy --strict over src and scripts
	$(UV) run mypy

test: ## Offline test suite with coverage; live and paid tests are skipped
	$(UV) run python -m pytest --cov --cov-report=term

eval-gate: ## Retrieval regression gate in the offline configuration, as CI runs it
	RAG_EMBEDDING_PROVIDER=hashing RAG_RERANKER=none $(UV) run rag eval 	  --thresholds eval/thresholds.json --output eval/reports

smoke: ## Start a real server; check readiness, ingestion, search and a cited answer
	$(UV) run python scripts/smoke_test.py

package: ## Build sdist and wheel, then install the wheel alone and run the CLI from it
	rm -rf dist
	$(UV) build
	$(UV) run python -m zipfile -l dist/*.whl | grep -q "production_rag/db/migrations/versions/"
	cd "$$(mktemp -d)" && export RAG_DATABASE_URL=sqlite:///./package-check.db && 	  $(UV) run --isolated --no-project --with "$(CURDIR)"/dist/*.whl rag db upgrade && 	  $(UV) run --isolated --no-project --with "$(CURDIR)"/dist/*.whl 	    rag ingest "$(CURDIR)/eval/corpus/deployment-runbook.md" && 	  $(UV) run --isolated --no-project --with "$(CURDIR)"/dist/*.whl 	    rag search "rollback command" | grep -q "deployment-runbook.md"
	@echo "package check passed"

check: lock-check lint typecheck test eval-gate ## Everything the CI quality job checks, except the smoke test

live-test: ## Free tests that download the embedding and reranking models (~150 MB)
	RAG_RUN_LIVE_TESTS=1 $(UV) run python -m pytest tests/integration/test_live.py

docs-lint: ## Markdown lint (needs Node.js); links are checked by the test suite
	npx --yes markdownlint-cli2 "**/*.md" "#.venv" "#node_modules"

serve: ## Run the API on http://127.0.0.1:8000 (interactive docs at /docs)
	$(UV) run rag serve

clean: ## Remove caches, coverage data and build output
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
