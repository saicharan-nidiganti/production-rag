"""Backups, verification, restores and retention (db/maintenance.py, `rag db ...`)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from production_rag.cli import main
from production_rag.config import Settings
from production_rag.container import Container, build_container
from production_rag.db import maintenance as m
from production_rag.db.documents import DocumentRepository
from production_rag.db.migrate import head_revision
from production_rag.ingestion import ExtractionError
from tests.conftest import CORPUS_DIR

RUNBOOK = (CORPUS_DIR / "deployment-runbook.md").read_bytes()
INCIDENTS = (CORPUS_DIR / "incident-response.md").read_bytes()
NO_TEXT = b"# Title\n\n## Nothing here\n"


def db_path(settings: Settings) -> Path:
    return m.database_file(settings.database_url)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sql(path: Path, statement: str, *params: object) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(statement, params).fetchall()
    conn.close()
    return rows


def failed(report: m.VerifyReport) -> set[str]:
    return {check.name for check in report.checks if not check.ok}


@pytest.fixture
def ingested(container: Container) -> Container:
    container.ingestion.ingest("runbook.md", RUNBOOK)
    return container


# --- Lineage ---


def test_each_document_records_what_produced_its_chunks(migrated_settings: Settings) -> None:
    from production_rag import __version__

    migrated_settings.chunk_size, migrated_settings.chunk_overlap = 400, 40
    container = build_container(migrated_settings)
    try:
        result = container.ingestion.ingest("runbook.md", RUNBOOK)
        document = container.documents.get(result.document.id)
    finally:
        container.close()
    assert document is not None
    assert document.metadata_["ingested_with"] == {
        "version": __version__,
        "chunk_size": 400,
        "chunk_overlap": 40,
    }


# --- Backups ---


def test_a_backup_is_a_verified_single_file_with_a_manifest(
    ingested: Container, tmp_path: Path
) -> None:
    made = m.backup(db_path(ingested.settings), tmp_path / "backups")

    assert made.path.parent == tmp_path / "backups"
    assert made.path.name.startswith(m.BACKUP_PREFIX)
    assert not made.path.with_name(made.path.name + "-wal").exists()
    assert sql(made.path, "PRAGMA journal_mode") == [("delete",)]  # no WAL to carry along
    manifest = json.loads(made.manifest_path.read_text(encoding="utf-8"))
    assert manifest == made.manifest
    assert manifest["sha256"] == sha256(made.path)
    assert manifest["size_bytes"] == made.path.stat().st_size
    assert manifest["schema_revision"] == head_revision()
    assert manifest["documents"] == {"queued": 0, "processing": 0, "ready": 1, "failed": 0}
    assert manifest["chunks"] == sql(made.path, "SELECT COUNT(*) FROM chunks")[0][0] > 0
    assert manifest["embedding_models"] == {ingested.embedder.model_id: manifest["chunks"]}

    report = m.verify(made.path)
    assert report.ok, report.as_dict()
    assert report.checks[0].name == "checksum"
    assert sha256(made.path) == manifest["sha256"]  # verifying didn't change the file


def test_a_backup_includes_writes_still_in_the_wal(container: Container, tmp_path: Path) -> None:
    database = db_path(container.settings)
    with container.engine.connect() as conn:  # keeps the WAL from being checkpointed
        container.ingestion.ingest("runbook.md", RUNBOOK)
        made = m.backup(database, tmp_path / "copy.db")
        conn.rollback()
    assert made.path == tmp_path / "copy.db"
    assert sql(made.path, "SELECT COUNT(*) FROM documents") == [(1,)]


def test_a_backup_never_overwrites_a_file(ingested: Container, tmp_path: Path) -> None:
    target = tmp_path / "copy.db"
    target.write_bytes(b"keep me")
    with pytest.raises(m.MaintenanceError, match="already exists"):
        m.backup(db_path(ingested.settings), target)
    assert target.read_bytes() == b"keep me"


def test_old_backups_are_pruned_and_other_files_left_alone(
    ingested: Container, tmp_path: Path
) -> None:
    directory = tmp_path / "backups"
    made = [m.backup(db_path(ingested.settings), directory) for _ in range(3)]
    unrelated = directory / "notes.db"
    unrelated.write_bytes(b"")

    removed = m.prune_backups(directory, keep=2)

    assert removed == [made[0].path]
    assert not made[0].manifest_path.exists()
    assert all(b.path.exists() and b.manifest_path.exists() for b in made[1:])
    assert unrelated.exists()
    with pytest.raises(m.MaintenanceError, match="at least one"):
        m.prune_backups(directory, keep=0)


# --- Verification ---


def test_the_live_database_passes(ingested: Container) -> None:
    report = m.verify(db_path(ingested.settings))
    assert report.ok, report.as_dict()
    assert report.schema_revision == head_revision()
    assert {c.name for c in report.checks} >= {"sqlite_integrity", "search_index", "uploads"}
    assert "checksum" not in {c.name for c in report.checks}  # no manifest beside it


def test_a_changed_backup_fails_its_checksum(ingested: Container, tmp_path: Path) -> None:
    made = m.backup(db_path(ingested.settings), tmp_path / "copy.db")
    with made.path.open("ab") as handle:
        handle.write(b"\0")
    assert failed(m.verify(made.path)) == {"checksum"}


def test_a_json_file_that_isnt_a_manifest_is_ignored(ingested: Container) -> None:
    database = db_path(ingested.settings)
    m.manifest_path(database).write_text('{"unrelated": true}', encoding="utf-8")
    assert m.verify(database).ok


def test_a_full_text_index_out_of_step_is_found_and_rebuilt(ingested: Container) -> None:
    database = db_path(ingested.settings)
    sql(database, "INSERT INTO chunks_fts(rowid, text) VALUES (99999, 'a ghost entry')")
    report = m.verify(database)
    assert failed(report) == {"search_index"}
    assert "rebuild-search-index" in next(c.detail for c in report.checks if not c.ok)

    m.rebuild_search_index(database)
    assert m.verify(database).ok
    hits = ingested.rag.retriever.search("canary", top_k=3, mode="lexical")
    assert hits.chunks  # search works from the rebuilt index


@pytest.mark.parametrize(
    ("damage", "check"),
    [
        ("UPDATE documents SET num_chunks = num_chunks + 1", "ready_documents"),
        ("DELETE FROM chunks", "ready_documents"),
        (
            "UPDATE chunks SET embedding = X'00' WHERE id = (SELECT MIN(id) FROM chunks)",
            "embeddings",
        ),
        ("DELETE FROM index_state", "index_state"),
    ],
    ids=["chunk-count", "missing-chunks", "vector-size", "index-state"],
)
def test_broken_invariants_are_reported(ingested: Container, damage: str, check: str) -> None:
    database = db_path(ingested.settings)
    sql(database, damage)
    assert check in failed(m.verify(database))


def test_chunks_and_uploads_must_match_each_status(ingested: Container) -> None:
    database = db_path(ingested.settings)
    sql(database, "UPDATE documents SET status = 'failed'")  # chunks, but no upload to retry
    assert failed(m.verify(database)) == {"partial_chunks", "uploads"}


def test_files_that_arent_production_rag_databases_fail(tmp_path: Path) -> None:
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all" * 100)
    assert failed(m.verify(junk)) == {"sqlite_integrity"}

    empty = tmp_path / "empty.db"
    sql(empty, "CREATE TABLE t (x)")
    report = m.verify(empty)
    assert failed(report) == {"schema"}
    assert "not a migrated" in report.checks[-1].detail

    with pytest.raises(m.MaintenanceError, match="no database file"):
        m.verify(tmp_path / "missing.db")


def test_schema_revisions(ingested: Container) -> None:
    database = db_path(ingested.settings)
    sql(database, "UPDATE alembic_version SET version_num = '0002'")
    behind = m.verify(database)
    assert behind.ok and behind.schema_revision == "0002"
    assert "rag db upgrade" in next(c.detail for c in behind.checks if c.name == "schema")

    sql(database, "UPDATE alembic_version SET version_num = '9999'")
    assert failed(m.verify(database)) == {"schema"}


# --- Restore ---


def test_a_restore_brings_back_the_backup_and_keeps_the_replaced_database(
    ingested: Container, tmp_path: Path
) -> None:
    database = db_path(ingested.settings)
    made = m.backup(database, tmp_path / "before.db")
    ingested.ingestion.ingest("incidents.md", INCIDENTS)
    before_restore = sql(database, "SELECT generation FROM index_state")[0][0]

    restored = m.restore(made.path, database)

    assert restored.report.ok and not restored.needs_upgrade
    assert sql(database, "SELECT source_name FROM documents") == [("runbook.md",)]
    assert restored.previous_copy is not None
    assert sorted(sql(restored.previous_copy, "SELECT source_name FROM documents")) == [
        ("incidents.md",),
        ("runbook.md",),
    ]
    generation = sql(database, "SELECT generation FROM index_state")[0][0]
    assert generation > max(
        before_restore, sql(made.path, "SELECT generation FROM index_state")[0][0]
    )
    assert sha256(made.path) == made.manifest["sha256"]  # the backup itself is untouched


def test_a_running_process_sees_the_restored_index(ingested: Container, tmp_path: Path) -> None:
    def sources() -> set[str]:
        hits = ingested.rag.retriever.search("incident", top_k=10, mode="hybrid").chunks
        return {hit.source_name for hit in hits}

    database = db_path(ingested.settings)
    made = m.backup(database, tmp_path / "before.db")
    ingested.ingestion.ingest("incidents.md", INCIDENTS)
    assert "incidents.md" in sources()  # now cached in this process's vector index

    m.restore(made.path, database)

    assert sources() == {"runbook.md"}


def test_a_backup_that_fails_verification_is_not_restored(
    ingested: Container, tmp_path: Path
) -> None:
    database = db_path(ingested.settings)
    made = m.backup(database, tmp_path / "copy.db")
    with made.path.open("ab") as handle:
        handle.write(b"\0")
    untouched = sha256(database)
    with pytest.raises(
        m.MaintenanceError, match=r"failed verification \(checksum\); nothing changed"
    ):
        m.restore(made.path, database)
    assert sha256(database) == untouched
    assert not list(database.parent.glob("*.pre-restore-*"))


def test_a_restore_onto_a_new_machine_and_from_an_older_schema(
    ingested: Container, tmp_path: Path
) -> None:
    made = m.backup(db_path(ingested.settings), tmp_path / "old.db")
    sql(made.path, "UPDATE alembic_version SET version_num = '0002'")
    made.manifest_path.unlink()  # as if made by an earlier version, without a manifest

    target = tmp_path / "new-machine" / "rag.db"
    restored = m.restore(made.path, target)

    assert restored.previous_copy is None
    assert restored.needs_upgrade
    assert restored.report.ok


def test_only_sqlite_files_can_be_backed_up() -> None:
    for url in ("sqlite://", "sqlite:///:memory:", "postgresql://localhost/rag"):
        with pytest.raises(m.MaintenanceError, match="SQLite database file"):
            m.database_file(url)


# --- Retention ---


def test_old_failed_documents_are_deleted_with_their_uploads(container: Container) -> None:
    with pytest.raises(ExtractionError):
        container.ingestion.ingest("old.md", NO_TEXT)
    with pytest.raises(ExtractionError):
        container.ingestion.ingest("recent.md", NO_TEXT + b"\n")
    container.ingestion.ingest("runbook.md", RUNBOOK)
    database = db_path(container.settings)
    sql(database, "UPDATE documents SET updated_at = '2026-01-01 00:00:00.000000'")
    sql(
        database,
        "UPDATE documents SET updated_at = ? WHERE source_name = 'recent.md'",
        (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.%f"),
    )
    repository = DocumentRepository(container.sessions)
    cutoff = datetime.now(UTC) - timedelta(days=30)
    old_id = sql(database, "SELECT id FROM documents WHERE source_name = 'old.md'")[0][0]

    assert repository.delete_failed(cutoff, dry_run=True) == [old_id]
    assert sql(database, "SELECT COUNT(*) FROM documents") == [(3,)]

    assert repository.delete_failed(cutoff) == [old_id]
    assert sorted(sql(database, "SELECT source_name, status FROM documents")) == [
        ("recent.md", "failed"),
        ("runbook.md", "ready"),  # ready documents are never pruned, however old
    ]
    assert sql(database, "SELECT COUNT(*) FROM document_files WHERE document_id = ?", old_id) == [
        (0,)
    ]
    assert m.verify(database).ok


# --- CLI ---


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, db_url: str, tmp_path: Path) -> Path:
    monkeypatch.setenv("RAG_DATABASE_URL", db_url)
    monkeypatch.setenv("RAG_LOG_LEVEL", "WARNING")
    assert main(["db", "upgrade"]) == 0
    assert main(["ingest", str(CORPUS_DIR / "deployment-runbook.md")]) == 0
    return m.database_file(db_url)


def out(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    text = capsys.readouterr().out
    return json.loads(text[text.index("{") :])  # type: ignore[no-any-return]


def test_cli_backup_verify_restore(cli: Path, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert main(["db", "backup", "--keep", "1"]) == 0
    made = out(capsys)
    backup = Path(str(made["backup"]))
    assert backup.parent == cli.parent / "backups" and made["pruned"] == []

    assert main(["db", "verify"]) == 0
    assert out(capsys)["ok"] is True
    assert main(["db", "verify", "--file", str(backup)]) == 0
    assert out(capsys)["ok"] is True

    assert main(["db", "restore", str(backup)]) == 2  # without --yes
    assert "Stop the service" in capsys.readouterr().err
    assert main(["db", "restore", str(backup), "--yes"]) == 0
    restored = out(capsys)
    assert restored["needs_upgrade"] is False and Path(str(restored["previous_copy"])).exists()

    assert main(["db", "rebuild-search-index"]) == 0
    assert out(capsys)["rebuilt"] == "search_index"


def test_cli_reports_failures_with_exit_codes(
    cli: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sql(cli, "UPDATE documents SET num_chunks = 0")
    capsys.readouterr()
    assert main(["db", "verify"]) == 1
    assert out(capsys)["ok"] is False
    assert main(["db", "restore", str(tmp_path / "missing.db"), "--yes"]) == 1
    assert "no database file" in capsys.readouterr().err


def test_cli_prune(cli: Path, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert main(["db", "prune", "--failed-older-than", "30", "--dry-run"]) == 0
    assert out(capsys) == {
        "dry_run": True,
        "failed_older_than_days": 30,
        "would_delete": 0,
        "document_ids": [],
    }
    with pytest.raises(SystemExit):
        main(["db", "prune", "--failed-older-than", "-1"])


def test_a_restored_container_answers(settings: Settings, tmp_path: Path) -> None:
    """The end-to-end recovery an operator runs: back up, lose the disk, restore, upgrade."""
    from production_rag.db.migrate import upgrade_to_head

    upgrade_to_head(settings.database_url)
    original = build_container(settings)
    try:
        original.ingestion.ingest("runbook.md", RUNBOOK)
        made = m.backup(db_path(settings), tmp_path / "offsite" / "rag.db")
    finally:
        original.close()

    elsewhere = Settings(
        _env_file=None, database_url=f"sqlite:///{(tmp_path / 'new' / 'rag.db').as_posix()}"
    )  # type: ignore[call-arg]
    m.restore(made.path, db_path(elsewhere))
    upgrade_to_head(elsewhere.database_url)
    recovered = build_container(elsewhere)
    try:
        answer = recovered.rag.answer("How long does the canary run?")
    finally:
        recovered.close()
    assert answer.answer.status == "answered" and answer.answer.citations
