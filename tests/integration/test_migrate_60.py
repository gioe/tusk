"""Integration test for migrate_60: add user_prompt_tokens / user_prompt_count to skill_runs.

Covers:
- both columns are added (nullable INTEGER, no default)
- pre-migration rows survive with NULL values
- schema version advances 59 → 60
- idempotent short-circuit on re-run when already at v60
"""

import importlib.util
import os
import sqlite3

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT_DIR = os.path.join(REPO_ROOT, "bin")


def _load_migrate():
    spec = importlib.util.spec_from_file_location(
        "tusk_migrate",
        os.path.join(SCRIPT_DIR, "tusk-migrate.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_migrate = _load_migrate()


@pytest.fixture()
def db_at_v59(db_path):
    """Reset the fresh-init DB back to version 59 so migrate_60 will run.

    Drops the new columns from skill_runs if present (fresh DBs ship with them
    as of v60) so the migration's ALTER TABLE path is exercised.
    """
    conn = sqlite3.connect(str(db_path))
    cols = [row[1] for row in conn.execute("PRAGMA table_info(skill_runs)").fetchall()]
    for col in ("user_prompt_tokens", "user_prompt_count"):
        if col in cols:
            conn.execute(f"ALTER TABLE skill_runs DROP COLUMN {col}")
    conn.execute("PRAGMA user_version = 59")
    conn.commit()
    conn.close()
    return str(db_path)


def _skill_runs_columns(db):
    conn = sqlite3.connect(db)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(skill_runs)").fetchall()]
    conn.close()
    return cols


class TestMigrate60:

    def test_adds_user_prompt_tokens_column(self, db_at_v59, config_path):
        tusk_migrate.migrate_60(db_at_v59, config_path, SCRIPT_DIR)
        assert "user_prompt_tokens" in _skill_runs_columns(db_at_v59)

    def test_adds_user_prompt_count_column(self, db_at_v59, config_path):
        tusk_migrate.migrate_60(db_at_v59, config_path, SCRIPT_DIR)
        assert "user_prompt_count" in _skill_runs_columns(db_at_v59)

    def test_pre_migration_rows_survive_with_null(self, db_at_v59, config_path):
        conn = sqlite3.connect(db_at_v59)
        conn.execute(
            "INSERT INTO skill_runs (skill_name, started_at) VALUES ('tusk', datetime('now'))"
        )
        conn.commit()
        run_id = conn.execute("SELECT id FROM skill_runs").fetchone()[0]
        conn.close()

        tusk_migrate.migrate_60(db_at_v59, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v59)
        row = conn.execute(
            "SELECT user_prompt_tokens, user_prompt_count FROM skill_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        conn.close()
        assert row == (None, None)

    def test_advances_schema_version_to_60(self, db_at_v59, config_path):
        assert tusk_migrate.get_version(db_at_v59) == 59
        tusk_migrate.migrate_60(db_at_v59, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db_at_v59) >= 60

    def test_idempotent_when_already_at_v60(self, db_path, config_path):
        """Fresh DB is already at v60+ with the columns present; re-running is a no-op."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 60")
        conn.commit()
        conn.close()

        version_before = tusk_migrate.get_version(str(db_path))
        cols_before = sorted(_skill_runs_columns(str(db_path)))

        tusk_migrate.migrate_60(str(db_path), config_path, SCRIPT_DIR)

        assert tusk_migrate.get_version(str(db_path)) == version_before
        assert sorted(_skill_runs_columns(str(db_path))) == cols_before
