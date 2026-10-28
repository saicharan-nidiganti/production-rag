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
        assert {"documents", "chunks", "index_state", "chunks_fts", "alembic_version"} <= tables
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
        assert current_revision(engine) == head_revision() == "0001"
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
