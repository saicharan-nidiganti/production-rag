# Local data

This folder is for **local development data only**. Its contents are git-ignored.

| Path | Purpose |
| --- | --- |
| `rag.db` | The default SQLite database (`RAG_DATABASE_URL=sqlite:///data/rag.db`), created by `rag db upgrade`, plus its `-wal` and `-shm` files |
| `raw/` | Original documents you keep locally for ingesting with `rag ingest data/raw` |
| `processed/` | Intermediate extraction or chunking output produced locally |

Rules:

- Never commit real or private documents. The repository is public.
- Test fixtures belong in `tests/`, generated in code or stored as files, and
  must be synthetic or public-domain, with their source and license recorded
  next to them.
- Evaluation corpora belong in `eval/corpus/`, with their source and license
  documented in [docs/evaluation.md](../docs/evaluation.md).
