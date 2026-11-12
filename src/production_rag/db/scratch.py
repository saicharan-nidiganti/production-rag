"""Throwaway databases for runs that build their own index: evaluation and the benchmark.

They run on the same kind of database as the configured one, so a PostgreSQL deployment is
measured on PostgreSQL. The configured database itself is never touched.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from production_rag.db.migrate import upgrade_to_head


class ScratchDatabaseError(RuntimeError):
    """A scratch database couldn't be created; the message says what the role needs."""


@contextmanager
def scratch_database(like: str) -> Iterator[str]:
    """The URL of a new, migrated, empty database of the same kind as ``like``, removed when
    the block ends. On PostgreSQL it is created on the same server, which needs a role
    allowed to create databases."""
    url = make_url(like)
    if url.get_backend_name() != "postgresql":
        # ignore_cleanup_errors: on Windows a lingering SQLite handle would otherwise fail.
        with tempfile.TemporaryDirectory(prefix="rag-scratch-", ignore_cleanup_errors=True) as tmp:
            scratch = f"sqlite:///{Path(tmp, 'scratch.db').as_posix()}"
            upgrade_to_head(scratch)
            yield scratch
        return

    name = f"rag_scratch_{uuid.uuid4().hex[:16]}"  # letters, digits and underscores only
    admin = create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except SQLAlchemyError as exc:
        admin.dispose()
        raise ScratchDatabaseError(
            f"couldn't create a scratch database on the configured PostgreSQL server "
            f"({type(exc).__name__}); the role needs CREATEDB and permission to create the "
            "vector extension"
        ) from None
    try:
        scratch = url.set(database=name).render_as_string(hide_password=False)
        upgrade_to_head(scratch)
        yield scratch
    finally:
        with admin.connect() as conn:
            # FORCE: an engine that wasn't disposed must not keep the database alive.
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
