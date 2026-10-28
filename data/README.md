# Local data

This folder is for **local development data only**. Its contents are git-ignored.

| Folder | Purpose |
| --- | --- |
| `raw/` | Original documents you upload or drop in for local testing |
| `processed/` | Intermediate extraction or chunking output produced locally |

Rules:

- Never commit real or private documents. The repository is public.
- Test fixtures belong in `tests/fixtures/`. Use synthetic or public-domain
  documents there, and record their source and license next to them.
- Evaluation corpora belong in `evaluations/`, with their source and license
  documented.
