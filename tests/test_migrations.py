"""Migration tests — focused on the callback_token → callback_nonce rename (#25).

The pantry's earlier migrations use Postgres-only column types (ARRAY), so we
can't replay the full chain against sqlite. We instead test the rename
migration in isolation: we hand-build the prior shape of `submissions`, run
the migration's upgrade()/downgrade() via a real alembic Operations context,
and assert the column rename + value preservation.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
    text,
)


RENAME_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "g4b5c6d7e8f9_rename_callback_token_to_callback_nonce.py"
)


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'migrate.db'}")
    md = MetaData()
    # Minimal pre-migration shape — just the columns the rename touches.
    Table(
        "submissions",
        md,
        Column("id", Integer, primary_key=True),
        Column("callback_token", String(64), nullable=True),
    )
    md.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "rename_callback_migration", RENAME_MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCallbackColumnRename:
    def test_upgrade_renames_callback_token_to_callback_nonce(self, engine):
        migration = _load_migration()
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()

        cols = _columns(engine, "submissions")
        assert "callback_nonce" in cols
        assert "callback_token" not in cols

    def test_downgrade_reverts_to_callback_token(self, engine):
        migration = _load_migration()
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.downgrade()

        cols = _columns(engine, "submissions")
        assert "callback_token" in cols
        assert "callback_nonce" not in cols

    def test_in_flight_rows_keep_their_nonce_value(self, engine):
        """A row that was already in awaiting_container before the migration
        ran must still find its nonce in the new column. Rename, not drop+add."""
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO submissions (id, callback_token) VALUES (1, 'in-flight-token')",
            ))

        migration = _load_migration()
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()

        with engine.begin() as conn:
            row = conn.execute(text(
                "SELECT callback_nonce FROM submissions WHERE id = 1",
            )).first()
        assert row is not None
        assert row[0] == "in-flight-token"

    def test_migration_uses_alter_column_not_drop_add(self):
        """Regression guard: rename must preserve in-flight rows. A drop+add
        pair would silently lose every awaiting_container row's value."""
        import inspect as _inspect
        migration = _load_migration()
        upgrade_src = _inspect.getsource(migration.upgrade)
        downgrade_src = _inspect.getsource(migration.downgrade)
        assert "alter_column" in upgrade_src
        assert "alter_column" in downgrade_src
        assert "drop_column" not in upgrade_src
        assert "drop_column" not in downgrade_src
        assert "add_column" not in upgrade_src
        assert "add_column" not in downgrade_src
