## Purpose

<!-- What problem does this change solve, and why now? -->

Closes #

## Implementation

<!-- The approach, and anything a reviewer should look at first. Alternatives you rejected, briefly. -->

## Type of change

- [ ] Feature
- [ ] Bug fix
- [ ] Documentation
- [ ] Refactor / chore
- [ ] Retrieval or generation experiment

## How was this tested?

<!--
Commands run and their results (`make check`, `make smoke`, ...). Distinguish unit
tests from integration and end-to-end checks. For bug fixes, name the regression test.
-->

## Evaluation impact

<!--
Required for changes to ingestion, chunking, embeddings, retrieval, prompts or models:
before/after metrics with the dataset version and configuration (report `config` block).
State whether results are offline or from a paid model run. Write "N/A" otherwise.
-->

## Risks and rollback

<!-- What could break, for whom, and how to undo it (migrations, settings, data). -->

## Checklist

- [ ] `make check` passes locally (lint, types, tests, retrieval gate)
- [ ] Tests added or updated, including a regression test for bug fixes
- [ ] Documentation updated where behavior or settings changed (`docs/configuration.md` for new settings)
- [ ] ADR added or updated for a consequential technical decision
- [ ] Schema changes come with an Alembic migration
- [ ] No paid API calls were added to CI or to the default test run
- [ ] No secrets, credentials or private documents committed
- [ ] No document contents, questions or personal data written to logs
