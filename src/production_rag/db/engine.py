"""Engine and session construction."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


def is_postgres(bind: Engine | Connection | Session) -> bool:
    """True when the code is talking to PostgreSQL, where both searches run as SQL."""
    engine = bind.get_bind() if isinstance(bind, Session) else bind
    return engine.dialect.name == "postgresql"


def create_db_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    backend = url.get_backend_name()
    if backend == "postgresql":
        # pool_pre_ping: a connection the server closed (a restart, an idle timeout) is
        # replaced instead of failing a request. hide_parameters: see below.
        return create_engine(url, pool_pre_ping=True, hide_parameters=True)
    if backend != "sqlite":
        raise ValueError(
            f"unsupported database {backend!r}: use sqlite:///path or "
            "postgresql+pg8000://user:password@host/database"
        )
    if url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)

    # hide_parameters: a failed statement's error message would otherwise quote its bound
    # values (document text, questions), and unexpected errors are logged in full (NFR-4).
    engine = create_engine(url, connect_args={"check_same_thread": False}, hide_parameters=True)

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn: sqlite3.Connection, _record: Any) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
