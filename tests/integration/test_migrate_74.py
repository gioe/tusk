"""Integration test for migrate_74: add input-token cache-split columns to
task_sessions and skill_runs.

Covers:
- schema version advances 73 -> 74
- task_sessions gains cache_read_tokens_in, cache_write_tokens_in, uncached_tokens_in
- skill_runs gains the same three columns
- pre-existing rows survive: NULL in the new columns, every other column unchanged
- idempotent short-circuit on re-run against a fresh v74 install

No tasks-table column changes, so the SELECT t.* view-recreation gotcha
(task_metrics / v_ready_tasks / v_chain_heads / v_criteria_coverage) does
not apply — no view snapshot guard is needed for this migration.

Issue #872.
"""

import importlib.util
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT_DIR = os.path.join(REPO_ROOT, "bin")

_NEW_COLUMNS = ("cache_read_tokens_in", "cache_write_tokens_in", "uncached_tokens_in")


def _load_migrate():
    spec = importlib.util.spec_from_file_location(
        "tusk_migrate",
        os.path.join(SCRIPT_DIR, "tusk-migrate.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_migrate = _load_migrate()


def _table_cols(db, table):
    conn = sqlite3.connect(db)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def _drop_new_columns_to_v73(db_path):
    """Reconstitute a v73 shape: drop the three new columns from both tables
    and stamp PRAGMA user_version = 73. Fresh installs ship v74+ so we have
    to walk it back to reproduce the migration target.

    SQLite 12-step ALTER TABLE pattern is overkill here — we use the
    rename/recreate/copy pattern instead since both tables are small enough
    that we re-list their columns inline.
    """
    db = str(db_path)
    conn = sqlite3.connect(db)
    # task_metrics joins task_sessions; SQLite refuses to rename a table
    # while a view still binds to it. Drop the view before the rebuild —
    # the migration we're testing doesn't depend on it and the test never
    # queries it back. (The bin/tusk-migrate.py regen_triggers final-step
    # runs after migrations and rebuilds nothing view-related, so leaving
    # task_metrics absent only affects subsequent ad-hoc reads, not the
    # migration itself.)
    conn.executescript(
        """
        DROP VIEW IF EXISTS v_velocity;
        DROP VIEW IF EXISTS v_blocked_tasks;
        DROP VIEW IF EXISTS task_metrics;
        BEGIN;
        ALTER TABLE task_sessions RENAME TO task_sessions_v74_pre;
        CREATE TABLE task_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            duration_seconds INTEGER,
            cost_dollars REAL,
            tokens_in INTEGER,
            tokens_out INTEGER,
            lines_added INTEGER,
            lines_removed INTEGER,
            model TEXT,
            agent_name TEXT,
            peak_context_tokens INTEGER,
            first_context_tokens INTEGER,
            last_context_tokens INTEGER,
            context_window INTEGER,
            request_count INTEGER,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        );
        INSERT INTO task_sessions (
            id, task_id, started_at, ended_at, duration_seconds, cost_dollars,
            tokens_in, tokens_out, lines_added, lines_removed, model, agent_name,
            peak_context_tokens, first_context_tokens, last_context_tokens,
            context_window, request_count
        )
        SELECT
            id, task_id, started_at, ended_at, duration_seconds, cost_dollars,
            tokens_in, tokens_out, lines_added, lines_removed, model, agent_name,
            peak_context_tokens, first_context_tokens, last_context_tokens,
            context_window, request_count
        FROM task_sessions_v74_pre;
        DROP TABLE task_sessions_v74_pre;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_sessions_open
            ON task_sessions(task_id) WHERE ended_at IS NULL;

        ALTER TABLE skill_runs RENAME TO skill_runs_v74_pre;
        CREATE TABLE skill_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at TEXT,
            cost_dollars REAL,
            tokens_in INTEGER,
            tokens_out INTEGER,
            model TEXT,
            metadata TEXT,
            request_count INTEGER,
            task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
            user_prompt_tokens INTEGER,
            user_prompt_count INTEGER
        );
        INSERT INTO skill_runs (
            id, skill_name, started_at, ended_at, cost_dollars, tokens_in,
            tokens_out, model, metadata, request_count, task_id,
            user_prompt_tokens, user_prompt_count
        )
        SELECT
            id, skill_name, started_at, ended_at, cost_dollars, tokens_in,
            tokens_out, model, metadata, request_count, task_id,
            user_prompt_tokens, user_prompt_count
        FROM skill_runs_v74_pre;
        DROP TABLE skill_runs_v74_pre;
        CREATE INDEX IF NOT EXISTS idx_skill_runs_skill_name
            ON skill_runs(skill_name);

        PRAGMA user_version = 73;
        COMMIT;
        """
    )
    conn.close()
    return db


class TestMigrate74:

    def test_advances_schema_version_to_74(self, db_path, config_path):
        db = _drop_new_columns_to_v73(db_path)
        assert tusk_migrate.get_version(db) == 73
        tusk_migrate.migrate_74(db, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db) == 74

    def test_adds_cache_split_columns_to_task_sessions(self, db_path, config_path):
        db = _drop_new_columns_to_v73(db_path)
        before = _table_cols(db, "task_sessions")
        for col in _NEW_COLUMNS:
            assert col not in before

        tusk_migrate.migrate_74(db, config_path, SCRIPT_DIR)

        after = _table_cols(db, "task_sessions")
        for col in _NEW_COLUMNS:
            assert col in after

    def test_adds_cache_split_columns_to_skill_runs(self, db_path, config_path):
        db = _drop_new_columns_to_v73(db_path)
        before = _table_cols(db, "skill_runs")
        for col in _NEW_COLUMNS:
            assert col not in before

        tusk_migrate.migrate_74(db, config_path, SCRIPT_DIR)

        after = _table_cols(db, "skill_runs")
        for col in _NEW_COLUMNS:
            assert col in after

    def test_preserves_existing_task_session_rows(self, db_path, config_path):
        """A pre-migration row must survive — keeping every legacy column
        value and acquiring NULL in each new cache-split column."""
        db = _drop_new_columns_to_v73(db_path)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO tasks (summary, status) VALUES ('legacy task', 'Done')"
        )
        task_id = conn.execute(
            "SELECT id FROM tasks WHERE summary = 'legacy task'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, tokens_in, tokens_out, cost_dollars, model) "
            "VALUES (?, '2026-01-01 00:00:00', 1234, 56, 0.42, 'claude-opus-4-7')",
            (task_id,),
        )
        session_id = conn.execute(
            "SELECT id FROM task_sessions WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        conn.commit()
        conn.close()

        tusk_migrate.migrate_74(db, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM task_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["tokens_in"] == 1234
        assert row["tokens_out"] == 56
        assert abs(row["cost_dollars"] - 0.42) < 1e-9
        assert row["model"] == "claude-opus-4-7"
        for col in _NEW_COLUMNS:
            assert row[col] is None

    def test_preserves_existing_skill_run_rows(self, db_path, config_path):
        db = _drop_new_columns_to_v73(db_path)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO skill_runs (skill_name, started_at, tokens_in, tokens_out, cost_dollars, model) "
            "VALUES ('tusk', '2026-01-01 00:00:00', 555, 7, 0.05, 'claude-opus-4-7')"
        )
        run_id = conn.execute(
            "SELECT id FROM skill_runs WHERE skill_name = 'tusk' AND started_at = '2026-01-01 00:00:00'"
        ).fetchone()[0]
        conn.commit()
        conn.close()

        tusk_migrate.migrate_74(db, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM skill_runs WHERE id = ?", (run_id,)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["tokens_in"] == 555
        assert row["tokens_out"] == 7
        assert abs(row["cost_dollars"] - 0.05) < 1e-9
        for col in _NEW_COLUMNS:
            assert row[col] is None

    def test_new_columns_accept_integer_values_post_migration(
        self, db_path, config_path
    ):
        """After migration, callers must be able to INSERT and SELECT
        explicit cache-split values."""
        db = _drop_new_columns_to_v73(db_path)
        tusk_migrate.migrate_74(db, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO tasks (summary, status) VALUES ('post-migration task', 'Done')"
        )
        task_id = conn.execute(
            "SELECT id FROM tasks WHERE summary = 'post-migration task'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, cache_read_tokens_in, "
            "cache_write_tokens_in, uncached_tokens_in) "
            "VALUES (?, '2026-01-02 00:00:00', 1000, 200, 50)",
            (task_id,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT cache_read_tokens_in, cache_write_tokens_in, uncached_tokens_in "
            "FROM task_sessions WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        conn.close()
        assert row["cache_read_tokens_in"] == 1000
        assert row["cache_write_tokens_in"] == 200
        assert row["uncached_tokens_in"] == 50

    def test_idempotent_when_already_at_v74(self, db_path, config_path):
        """Fresh DB ships at v74+. Stamping to 74 explicitly keeps the test
        future-proof across later migrations; migrate_74 must short-circuit
        without touching any column or bumping the version. Per CLAUDE.md
        migration template: stamp explicitly, then assert preservation."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 74")
        conn.commit()
        conn.close()

        version_before = tusk_migrate.get_version(str(db_path))
        ts_cols_before = _table_cols(str(db_path), "task_sessions")
        sr_cols_before = _table_cols(str(db_path), "skill_runs")

        tusk_migrate.migrate_74(str(db_path), config_path, SCRIPT_DIR)

        assert tusk_migrate.get_version(str(db_path)) == version_before
        assert _table_cols(str(db_path), "task_sessions") == ts_cols_before
        assert _table_cols(str(db_path), "skill_runs") == sr_cols_before

    def test_idempotent_partial_run_safe(self, db_path, config_path):
        """If migrate_74 runs once and is invoked again on a v74 DB, all
        ALTER TABLE statements should short-circuit (column already exists)
        without error and version stays at 74."""
        db = _drop_new_columns_to_v73(db_path)

        tusk_migrate.migrate_74(db, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db) == 74

        tusk_migrate.migrate_74(db, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db) == 74

        cols_ts = _table_cols(db, "task_sessions")
        cols_sr = _table_cols(db, "skill_runs")
        for col in _NEW_COLUMNS:
            assert col in cols_ts
            assert col in cols_sr
