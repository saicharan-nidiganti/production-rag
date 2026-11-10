"""Backups, integrity checks and restores of the SQLite database (docs/data-lifecycle.md).

Everything here works on the database file directly, through Python's ``sqlite3``, so it
also works on a backup that no engine has opened, and inside the container image, which
has no ``sqlite3`` shell.

- A **backup** is a consistent online copy made with SQLite's backup API, which includes
  what is still in the write-ahead log. It is a single file (rollback-journal mode), checked
  with ``PRAGMA integrity_check`` before it is given its final name, and has a JSON manifest
  beside it: when and by which version it was made, its schema revision, counts, and the
  file's SHA-256.
- **Verification** checks a database or a backup: the manifest's checksum first, so a check
  can't mask a changed file, then SQLite's own integrity and foreign-key checks, the schema
  revision, the full-text index against its table, and the invariants the ingestion code
  keeps. None of it writes to the file.
- A **restore** verifies the backup, keeps a copy of the database it replaces, copies the
  backup in with the same API, moves the index generation forward so any process that still
  caches vectors reloads them, and verifies the result.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

from production_rag import __version__
from production_rag.db.migrate import head_revision, known_revisions

logger = logging.getLogger(__name__)

MANIFEST_FORMAT = 1
BACKUP_PREFIX = "rag-backup-"
_STAMP = "%Y%m%d-%H%M%S-%f"


class MaintenanceError(RuntimeError):
    """A maintenance operation was refused or failed; the message says why."""


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class VerifyReport:
    path: Path
    checks: list[Check] = field(default_factory=list)
    schema_revision: str | None = None

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "ok": self.ok,
            "schema_revision": self.schema_revision,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
        }


@dataclass(frozen=True)
class Backup:
    path: Path
    manifest_path: Path
    manifest: dict[str, Any]


@dataclass(frozen=True)
class Restore:
    backup: Path
    database: Path
    previous_copy: Path | None
    report: VerifyReport

    @property
    def needs_upgrade(self) -> bool:
        return self.report.schema_revision != head_revision()


def database_file(database_url: str) -> Path:
    """The SQLite file a ``RAG_DATABASE_URL`` points at."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise MaintenanceError("backups and restores need a SQLite database file")
    return Path(url.database)


def manifest_path(backup: Path) -> Path:
    return backup.with_suffix(".json")


# --- Backups ---


def backup(database: Path, destination: Path | None = None) -> Backup:
    """Copy ``database`` to ``destination``: a directory (default ``<db dir>/backups``),
    which gets a time-stamped file, or a file path ending in ``.db``."""
    _require(database)
    created = datetime.now(UTC)
    target = destination if destination is not None else database.parent / "backups"
    if target.suffix != ".db":
        target = target / f"{BACKUP_PREFIX}{created.strftime(_STAMP)[:-3]}.db"
    if target.exists():
        raise MaintenanceError(f"{target} already exists; choose another name")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)
    try:
        _copy(database, partial, journal_mode="DELETE")
        with _connect(partial) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise MaintenanceError(f"the copy failed SQLite's integrity check: {result}")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)

    with _connect(target) as conn:
        manifest: dict[str, Any] = {
            "format": MANIFEST_FORMAT,
            "created_at": created.isoformat(),
            "app_version": __version__,
            "sqlite_version": sqlite3.sqlite_version,
            "source": database.name,
            "schema_revision": _revision(conn),
            **_counts(conn),
        }
    manifest["size_bytes"] = target.stat().st_size
    manifest["sha256"] = _sha256(target)
    manifest_file = manifest_path(target)
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    logger.info(
        "backup.created",
        extra={
            "backup": target.name,
            "size_bytes": manifest["size_bytes"],
            "schema_revision": manifest["schema_revision"],
            "chunks": manifest["chunks"],
        },
    )
    return Backup(target, manifest_file, manifest)


def prune_backups(directory: Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` time-stamped backups in ``directory``, with their
    manifests. Files that don't follow the backup naming are never touched."""
    if keep < 1:
        raise MaintenanceError("keep at least one backup")
    backups = sorted(directory.glob(f"{BACKUP_PREFIX}*.db"))  # time stamps sort in order
    removed = backups[:-keep]
    for path in removed:
        path.unlink()
        manifest_path(path).unlink(missing_ok=True)
    if removed:
        logger.info("backup.pruned", extra={"removed": len(removed), "kept": keep})
    return removed


# --- Verification ---


def verify(path: Path) -> VerifyReport:
    """Check a database or a backup without modifying it."""
    _require(path)
    checks: list[Check] = []
    if (checksum := _check_manifest(path)) is not None:
        checks.append(checksum)
    revision = None
    with _connect(path) as conn:
        try:
            result = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        except sqlite3.DatabaseError as exc:  # not a database at all, or unreadable
            result = [str(exc)]
        checks.append(Check("sqlite_integrity", result == ["ok"], "; ".join(result[:5])))
        if result != ["ok"]:
            return _report(path, checks, revision)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        checks.append(
            Check("foreign_keys", not violations, f"{len(violations)} rows reference nothing")
        )
        schema, revision = _check_schema(conn)
        checks.append(schema)
        if schema.ok:  # the tables match what the remaining checks query
            checks.extend(_check_index(conn))
    return _report(path, checks, revision)


def _check_manifest(path: Path) -> Check | None:
    """The checksum check, if ``path`` has a backup manifest beside it."""
    try:
        manifest = json.loads(manifest_path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        return Check("checksum", False, f"unreadable manifest: {type(exc).__name__}")
    if not isinstance(manifest, dict) or "format" not in manifest:
        return None  # some other JSON file that happens to share the name
    expected = manifest.get("sha256")
    if not isinstance(expected, str):
        return Check("checksum", False, "the manifest has no sha256")
    actual = _sha256(path)
    if actual != expected:
        return Check("checksum", False, "the file changed since the backup was made")
    return Check("checksum", True, f"sha256 {actual} matches the manifest")


def _check_schema(conn: sqlite3.Connection) -> tuple[Check, str | None]:
    try:
        revision = _revision(conn)
    except sqlite3.DatabaseError:
        revision = None
    head = head_revision()
    if revision is None:
        return Check("schema", False, "not a migrated production-rag database"), None
    if revision == head:
        return Check("schema", True, f"{revision}, the latest"), revision
    if revision in known_revisions():
        return Check("schema", True, f"{revision}, behind {head}: run `rag db upgrade`"), revision
    return Check("schema", False, f"{revision} is newer than this version ({head})"), revision


def _check_index(conn: sqlite3.Connection) -> list[Check]:
    def count(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    checks = []
    try:
        # rank 1 also compares the index with the chunks table it was built from.
        conn.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES ('integrity-check', 1)")
        checks.append(Check("search_index", True, "the full-text index matches the chunks"))
    except sqlite3.DatabaseError as exc:
        checks.append(
            Check("search_index", False, f"{exc}; repair it with `rag db rebuild-search-index`")
        )

    wrong_counts = count(
        "SELECT COUNT(*) FROM documents d WHERE d.status = 'ready' AND d.num_chunks != "
        "(SELECT COUNT(*) FROM chunks c WHERE c.document_id = d.id)"
    )
    empty = count(
        "SELECT COUNT(*) FROM documents d WHERE d.status = 'ready' "
        "AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id)"
    )
    checks.append(
        Check(
            "ready_documents",
            wrong_counts == 0 and empty == 0,
            f"{wrong_counts} with a chunk count that doesn't match, {empty} without chunks",
        )
    )
    unfinished = count(
        "SELECT COUNT(DISTINCT c.document_id) FROM chunks c "
        "JOIN documents d ON d.id = c.document_id WHERE d.status != 'ready'"
    )
    checks.append(
        Check("partial_chunks", unfinished == 0, f"{unfinished} unfinished documents have chunks")
    )
    without_upload = count(
        "SELECT COUNT(*) FROM documents d WHERE d.status IN ('queued', 'processing', 'failed') "
        "AND NOT EXISTS (SELECT 1 FROM document_files f WHERE f.document_id = d.id)"
    )
    leftover = count(
        "SELECT COUNT(*) FROM document_files f JOIN documents d ON d.id = f.document_id "
        "WHERE d.status = 'ready'"
    )
    checks.append(
        Check(
            "uploads",
            without_upload == 0 and leftover == 0,
            f"{without_upload} unfinished documents without their upload, "
            f"{leftover} ready documents still holding theirs",
        )
    )
    mixed = [
        model
        for model, sizes in conn.execute(
            "SELECT embedding_model, COUNT(DISTINCT length(embedding)) FROM chunks "
            "GROUP BY embedding_model"
        )
        if sizes != 1
    ]
    checks.append(
        Check(
            "embeddings",
            not mixed,
            f"vectors of different sizes under {', '.join(mixed)}"
            if mixed
            else "one vector size per embedding model",
        )
    )
    state = count("SELECT COUNT(*) FROM index_state WHERE id = 1")
    checks.append(Check("index_state", state == 1, f"{state} generation rows (expected 1)"))
    return checks


def _report(path: Path, checks: list[Check], revision: str | None) -> VerifyReport:
    report = VerifyReport(path, checks, revision)
    failed = [check.name for check in checks if not check.ok]
    logger.log(
        logging.INFO if report.ok else logging.WARNING,
        "db.verified",
        extra={"file": path.name, "ok": report.ok, "failed_checks": failed},
    )
    return report


# --- Restore and repair ---


def restore(backup_file: Path, database: Path) -> Restore:
    """Replace ``database`` with ``backup_file``. Stop the service first."""
    report = verify(backup_file)
    if not report.ok:
        failed = ", ".join(check.name for check in report.checks if not check.ok)
        raise MaintenanceError(f"{backup_file} failed verification ({failed}); nothing changed")

    previous_copy = None
    generation = 0
    if database.exists():
        with _connect(database) as conn:
            generation = _generation(conn)
        stamp = datetime.now(UTC).strftime(_STAMP)[:-3]
        previous_copy = database.with_name(f"{database.stem}.pre-restore-{stamp}.db")
        _copy(database, previous_copy, journal_mode="DELETE")
    database.parent.mkdir(parents=True, exist_ok=True)
    _copy(backup_file, database)
    with _connect(database) as conn:
        # A process that cached vectors at the old generation must not mistake the restored
        # index for the one it has: move past both.
        conn.execute(
            "UPDATE index_state SET generation = ? WHERE id = 1",
            (max(generation, _generation(conn)) + 1,),
        )
        conn.commit()

    result = Restore(backup_file, database, previous_copy, verify(database))
    logger.info(
        "restore.completed",
        extra={
            "backup": backup_file.name,
            "schema_revision": result.report.schema_revision,
            "previous_copy": previous_copy.name if previous_copy else None,
            "ok": result.report.ok,
        },
    )
    return result


def rebuild_search_index(database: Path) -> None:
    """Rebuild the full-text index from the chunks table, for example after corruption."""
    _require(database)
    with _connect(database) as conn:
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
        conn.execute("UPDATE index_state SET generation = generation + 1 WHERE id = 1")
        conn.commit()
    logger.info("search_index.rebuilt", extra={"file": database.name})


# --- Helpers ---


def _require(path: Path) -> None:
    if not path.is_file():
        raise MaintenanceError(f"no database file at {path}")


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    # sqlite3.connect would create a missing file; callers check that it exists first.
    with closing(sqlite3.connect(path, timeout=30)) as conn:
        yield conn


def _copy(source: Path, destination: Path, *, journal_mode: str | None = None) -> None:
    with _connect(source) as src, _connect(destination) as dst:
        src.backup(dst)
        if journal_mode:
            dst.execute(f"PRAGMA journal_mode={journal_mode}")


def _revision(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return str(row[0]) if row else None


def _generation(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT generation FROM index_state WHERE id = 1").fetchone()
    return int(row[0]) if row else 0


def _counts(conn: sqlite3.Connection) -> dict[str, Any]:
    by_status = dict(conn.execute("SELECT status, COUNT(*) FROM documents GROUP BY status"))
    models = dict(
        conn.execute("SELECT embedding_model, COUNT(*) FROM chunks GROUP BY embedding_model")
    )
    return {
        "documents": {
            status: by_status.get(status, 0)
            for status in ("queued", "processing", "ready", "failed")
        },
        "chunks": sum(models.values()),
        "embedding_models": models,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
