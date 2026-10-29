"""The technology choices fit the architecture (issue #3): checked on every CI Python version.

The architecture is one local-first Python process on SQLite (ADR-0002). These tests check
what that depends on at runtime and keep the supported-version claims consistent; the
assessment itself is in docs/architecture.md, section 11.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import tomllib
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
MINIMUM = PYPROJECT["project"]["requires-python"].removeprefix(">=")  # "3.12"


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def test_every_place_that_states_the_minimum_python_agrees() -> None:
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    matrix = re.search(r"python-version: \[([^\]]+)\]", ci)
    assert matrix is not None
    tested = [v.strip().strip('"') for v in matrix.group(1).split(",")]

    assert f'requires-python = ">={MINIMUM}"' in lock
    assert PYPROJECT["tool"]["ruff"]["target-version"] == "py" + MINIMUM.replace(".", "")
    assert PYPROJECT["tool"]["mypy"]["python_version"] == MINIMUM
    assert min(tested, key=_version) == MINIMUM  # the oldest supported version is tested
    assert f'python-version: "{MINIMUM}" # the oldest supported version' in ci  # package job
    assert sys.version_info >= _version(MINIMUM)


def test_sqlite_has_what_the_schema_and_migrations_use(tmp_path: Path) -> None:
    # ALTER TABLE ... DROP COLUMN, used by the 0002 and 0003 downgrades, needs SQLite 3.35.
    assert _version(sqlite3.sqlite_version) >= (3, 35)
    with sqlite3.connect(tmp_path / "check.db") as conn:
        # Foreign keys with cascades: deleting a document removes its chunks and upload.
        # Set before any statement, as db/engine.py does on connect: SQLite ignores the
        # pragma inside a transaction.
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        # WAL: queries keep reading while ingestion writes (ADR-0002, ADR-0006).
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        # FTS5 with the Porter stemmer: lexical search (migration 0001).
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(text, tokenize='porter unicode61')")
        conn.execute("INSERT INTO t(text) VALUES ('rolling back a release')")
        # The stemmer matches "rolling" for "roll".
        assert conn.execute("SELECT count(*) FROM t WHERE t MATCH 'roll'").fetchone()[0] == 1


def test_the_semantic_extra_installs_on_this_python() -> None:
    """fastembed and onnxruntime must have wheels for every supported Python version.

    CI installs every extra (make install), so there a missing wheel fails here instead of
    being skipped. Importing loads no model: nothing is downloaded.
    """
    if os.environ.get("CI"):
        import fastembed  # noqa: F401
        import onnxruntime  # noqa: F401
    else:
        pytest.importorskip("fastembed")
        pytest.importorskip("onnxruntime")
