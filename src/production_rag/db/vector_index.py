"""The approximate vector index on PostgreSQL: one HNSW index per embedding model.

``chunks.embedding`` has no fixed dimension, since models differ, and HNSW needs one. So
each index is a partial expression index: it covers one model's rows, cast to that model's
dimension, and a search uses it only when it orders by the same expression with the same
model condition (``retrieval/vector.py``). ADR-0014 records the measurements behind it.

Indexes are built and dropped with ``CONCURRENTLY``, so writes continue meanwhile. That
can't run inside a transaction, so these functions use their own autocommit engine rather
than the application's.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)

# Iterative index scans arrived in 0.8.0. Without them a search limited to one owner can
# return fewer results than asked for, because the owner filter is applied after the scan.
MIN_PGVECTOR = (0, 8, 0)
# The build parameters: wider than pgvector's defaults (16 and 64), which lost answers that
# exact search found in the measurements of ADR-0014, for about three times the build time.
DEFAULT_M = 32
DEFAULT_EF_CONSTRUCTION = 128
_PGVECTOR_M, _PGVECTOR_EF_CONSTRUCTION = 16, 64  # what an index built without options has
_NAME_PREFIX = "ix_chunks_hnsw_"
_MEMORY = re.compile(r"^[1-9][0-9]{0,6}(kB|MB|GB)$")


class VectorIndexError(RuntimeError):
    """The index can't be built, dropped or used; the message says what to change."""


@dataclass(frozen=True)
class IndexStatus:
    model: str
    name: str
    exists: bool
    valid: bool = False
    dimensions: int | None = None
    size_bytes: int | None = None
    m: int | None = None
    ef_construction: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def index_name(model_id: str) -> str:
    """A stable name for the model's index, within PostgreSQL's 63-character limit."""
    return _NAME_PREFIX + hashlib.sha256(model_id.encode()).hexdigest()[:16]


def pgvector_version(conn: Connection) -> tuple[int, ...] | None:
    """The installed version of the ``vector`` extension, or None if it isn't installed."""
    version = conn.execute(
        text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    ).scalar()
    if version is None:
        return None
    return tuple(int(part) for part in re.findall(r"\d+", str(version))[:3])


def check_pgvector(conn: Connection) -> str:
    """The pgvector version as text; VectorIndexError if it can't serve approximate search."""
    version = pgvector_version(conn)
    if version is None:
        raise VectorIndexError("the vector extension isn't installed: run `rag db upgrade`")
    shown = ".".join(str(part) for part in version)
    if version < MIN_PGVECTOR:
        raise VectorIndexError(
            f"approximate search needs pgvector 0.8.0 or later for iterative index scans; "
            f"this server has {shown}. Upgrade pgvector, or set RAG_VECTOR_SEARCH=exact"
        )
    return shown


def status(engine: Engine, model_id: str) -> IndexStatus:
    """Whether the model's index exists, is valid (a failed concurrent build leaves an
    invalid one), and its size and build parameters."""
    name = index_name(model_id)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT i.indisvalid AS valid, pg_relation_size(i.indexrelid) AS size, "
                "c.reloptions AS options, pg_get_indexdef(i.indexrelid) AS definition "
                "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name"
            ),
            {"name": name},
        ).first()
    if row is None:
        return IndexStatus(model=model_id, name=name, exists=False)
    options = dict(option.split("=", 1) for option in row.options or [])
    dimensions = re.search(r"::vector\((\d+)\)", row.definition)
    return IndexStatus(
        model=model_id,
        name=name,
        exists=True,
        valid=bool(row.valid),
        dimensions=int(dimensions.group(1)) if dimensions else None,
        size_bytes=int(row.size),
        m=int(options.get("m", _PGVECTOR_M)),
        ef_construction=int(options.get("ef_construction", _PGVECTOR_EF_CONSTRUCTION)),
    )


def readiness(engine: Engine, model_id: str) -> tuple[bool, dict[str, Any]]:
    """For ``/ready`` with approximate search. Not ready when pgvector can't run it. A
    missing or invalid index is reported but doesn't block: searches are then exact, which
    is correct, only slower."""
    with engine.connect() as conn:
        try:
            version = check_pgvector(conn)
        except VectorIndexError as exc:
            return False, {"search": "approximate", "error": str(exc)}
    current = status(engine, model_id)
    state = "valid" if current.valid else "invalid" if current.exists else "missing"
    detail: dict[str, Any] = {"search": "approximate", "pgvector": version, "index": state}
    if state != "valid":
        detail["hint"] = "run `rag db create-vector-index`; searches are exact until then"
    return True, detail


def create(
    database_url: str,
    model_id: str,
    dimensions: int,
    *,
    m: int = DEFAULT_M,
    ef_construction: int = DEFAULT_EF_CONSTRUCTION,
    maintenance_work_mem: str | None = None,
) -> dict[str, Any]:
    """Build the model's HNSW index, unless a valid one exists. An invalid index left by a
    failed build is dropped and rebuilt. Returns what was done and how long it took."""
    _check_parameters(dimensions, m, ef_construction, maintenance_work_mem)
    engine = _autocommit_engine(database_url)
    name = index_name(model_id)
    try:
        current = status(engine, model_id)
        if current.exists and current.valid:
            if current.dimensions != dimensions:
                raise VectorIndexError(
                    f"the index for {model_id} has {current.dimensions} dimensions, but the "
                    f"model has {dimensions}: drop it with `rag db drop-vector-index` first"
                )
            return {"created": False, **current.as_dict()}
        with engine.connect() as conn:
            check_pgvector(conn)
            stored = conn.execute(
                text(
                    "SELECT vector_dims(embedding) FROM chunks "
                    "WHERE embedding_model = :model LIMIT 1"
                ),
                {"model": model_id},
            ).scalar()
            if stored is not None and stored != dimensions:
                raise VectorIndexError(
                    f"the stored chunks of {model_id} have {stored} dimensions, not "
                    f"{dimensions}: run `rag reindex` first"
                )
            if current.exists:  # invalid: a concurrent build failed part way
                conn.execute(text(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"'))
            if maintenance_work_mem:
                conn.execute(text(f"SET maintenance_work_mem = '{maintenance_work_mem}'"))
            start = time.perf_counter()
            conn.execute(text(_create_statement(name, model_id, dimensions, m, ef_construction)))
            seconds = round(time.perf_counter() - start, 3)
            # The planner weighs the index against the owner filter: it needs both tables.
            conn.execute(text("ANALYZE documents, chunks"))
        built = status(engine, model_id)
        logger.info(
            "vector_index.created",
            extra={"model": model_id, "index": name, "seconds": seconds,
                   "size_bytes": built.size_bytes},
        )  # fmt: skip
        return {"created": True, "seconds": seconds, **built.as_dict()}
    finally:
        engine.dispose()


def drop(database_url: str, model_id: str) -> bool:
    """Drop the model's index; False if there was none. Searches carry on, exactly."""
    engine = _autocommit_engine(database_url)
    try:
        existed = status(engine, model_id).exists
        with engine.connect() as conn:
            conn.execute(text(f'DROP INDEX CONCURRENTLY IF EXISTS "{index_name(model_id)}"'))
        if existed:
            logger.info("vector_index.dropped", extra={"model": model_id})
        return existed
    finally:
        engine.dispose()


def _create_statement(name: str, model_id: str, dimensions: int, m: int, ef: int) -> str:
    # The model goes into the index predicate as a literal: DDL takes no parameters. It is
    # quoted by doubling single quotes, which is complete with standard_conforming_strings,
    # on by default since PostgreSQL 9.1. Model ids come from the operator's settings.
    model = model_id.replace("'", "''")
    return (
        f'CREATE INDEX CONCURRENTLY "{name}" ON chunks USING hnsw '
        f"((embedding::vector({dimensions})) vector_ip_ops) "
        f"WITH (m = {m}, ef_construction = {ef}) WHERE embedding_model = '{model}'"
    )


def _check_parameters(dimensions: int, m: int, ef: int, memory: str | None) -> None:
    # pgvector's own limits: HNSW indexes vectors of up to 2,000 dimensions, m is 2 to 100,
    # and ef_construction is 4 to 1,000 and at least twice m.
    if not 1 <= dimensions <= 2000:
        raise VectorIndexError(f"HNSW indexes 1 to 2,000 dimensions, not {dimensions}")
    if not 2 <= m <= 100:
        raise VectorIndexError(f"m must be 2 to 100, not {m}")
    if not 4 <= ef <= 1000 or ef < 2 * m:
        raise VectorIndexError(f"ef_construction must be 4 to 1,000 and at least 2 x m, not {ef}")
    if memory is not None and not _MEMORY.match(memory):
        raise VectorIndexError(f"maintenance_work_mem must look like 512MB or 2GB, not {memory!r}")


def _autocommit_engine(database_url: str) -> Engine:
    if make_url(database_url).get_backend_name() != "postgresql":
        raise VectorIndexError(
            "the vector index is for PostgreSQL; on SQLite the search is exact, in memory"
        )
    return create_engine(database_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
