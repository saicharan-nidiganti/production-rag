from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text

from production_rag.config import Settings
from production_rag.db import create_db_engine
from production_rag.db.migrate import alembic_config, current_revision, head_revision, is_at_head
from production_rag.db.models import Base


def test_upgrade_creates_schema_fts_and_triggers(migrated_settings: Settings) -> None:
    engine = create_db_engine(migrated_settings.database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {
            "documents",
            "document_files",
            "chunks",
            "index_state",
            "chunks_fts",
            "alembic_version",
        } <= tables
        with engine.connect() as conn:
            triggers = {
                r[0]
                for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))
            }
            generation = conn.execute(
                text("SELECT generation FROM index_state WHERE id = 1")
            ).scalar()
            fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
        assert triggers == {"chunks_ai", "chunks_ad", "chunks_au"}
        assert generation == 0
        assert fk == 1
        assert is_at_head(engine)
        assert current_revision(engine) == head_revision() == "0003"
    finally:
        engine.dispose()


def test_orm_models_match_the_migrations(migrated_settings: Settings) -> None:
    """Fails if a model changes without a matching migration."""
    engine = create_db_engine(migrated_settings.database_url)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn,
                opts={
                    "include_object": lambda obj, name, type_, reflected, compare_to: (
                        not (type_ == "table" and name.startswith("chunks_fts"))
                    )
                },
            )
            assert compare_metadata(context, Base.metadata) == []
    finally:
        engine.dispose()


def test_downgrade_removes_everything(migrated_settings: Settings) -> None:
    command.downgrade(alembic_config(migrated_settings.database_url), "base")
    engine = create_db_engine(migrated_settings.database_url)
    try:
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
        assert not is_at_head(engine)
    finally:
        engine.dispose()


def test_unmigrated_database_is_not_at_head(settings: Settings) -> None:
    engine = create_db_engine(settings.database_url)
    try:
        assert current_revision(engine) is None
        assert not is_at_head(engine)
    finally:
        engine.dispose()


def test_upgrading_keeps_existing_documents_ready(settings: Settings) -> None:
    """Documents ingested before revision 0002 keep their chunks and become ready."""
    config = alembic_config(settings.database_url)
    command.upgrade(config, "0001")
    engine = create_db_engine(settings.database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO documents (id, source_name, title, content_type, content_hash,"
                    " size_bytes, num_chunks, text, metadata, created_at) VALUES ('d1', 'a.md',"
                    " 'A', 'text/markdown', 'h1', 5, 1, 'hello', '{}',"
                    " '2026-10-28 10:00:00.000000')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO chunks (document_id, ordinal, text, start_char, end_char,"
                    " embedding, embedding_model) VALUES ('d1', 0, 'hello', 0, 5, x'00', 'm')"
                )
            )
        command.upgrade(config, "head")
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT status, attempts, updated_at, error_code FROM documents")
            ).one()
            chunks = conn.execute(text("SELECT count(*) FROM chunks")).scalar()
        assert tuple(row) == ("ready", 1, "2026-10-28 10:00:00.000000", None)
        assert chunks == 1

        # Downgrading drops documents revision 0001 can't represent, and keeps the rest.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO documents (id, source_name, title, content_type, content_hash,"
                    " size_bytes, num_chunks, text, metadata, created_at, status, attempts,"
                    " updated_at) VALUES ('d2', 'b.md', 'b', 'text/markdown', 'h2', 5, 0, '',"
                    " '{}', '2026-10-29 10:00:00', 'queued', 0, '2026-10-29 10:00:00')"
                )
            )
        command.downgrade(config, "0001")
        with engine.connect() as conn:
            ids = [r[0] for r in conn.execute(text("SELECT id FROM documents"))]
            columns = {c["name"] for c in inspect(conn).get_columns("documents")}
            chunks = conn.execute(text("SELECT count(*) FROM chunks")).scalar()
        assert ids == ["d1"] and chunks == 1
        assert "status" not in columns and "document_files" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
