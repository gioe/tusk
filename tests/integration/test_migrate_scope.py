"""Integration test for migrate_73: promote scope to a first-class concept.

Covers:
- ``task_scope`` table is created with the expected column shape
- existing tasks with referenced paths get backfilled as ``auto_derived``
  rows; tasks with no scope signal get no rows
- ``tasks.scope_enforced`` is added with DEFAULT 1, but existing rows are
  forced to 0 so already-running tasks keep their inferred-scope behavior
- a task inserted *after* migrate_73 picks up the DEFAULT 1
- idempotent short-circuit on re-run against a v73 install

The test simulates a pre-v73 DB by dropping the ``scope_enforced`` column
from the live tasks table (SQLite 12-step ALTER TABLE pattern), dropping
``task_scope`` if present, and stamping ``PRAGMA user_version = 72`` —
fresh fixtures ship at v73+ via ``cmd_init`` so this is the only way to
reproduce the migrated-DB pathway from a clean tusk install.
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
def db_at_v72(db_path):
    """Reset a fresh DB back to v72 shape: drop task_scope, drop the
    scope_enforced column from tasks, stamp user_version=72."""
    db = str(db_path)
    conn = sqlite3.connect(db)

    conn.executescript(
        """
        DROP TABLE IF EXISTS task_scope;

        DROP VIEW IF EXISTS v_velocity;
        DROP VIEW IF EXISTS v_blocked_tasks;
        DROP VIEW IF EXISTS task_metrics;
        DROP VIEW IF EXISTS v_ready_tasks;
        DROP VIEW IF EXISTS v_chain_heads;
        DROP VIEW IF EXISTS v_criteria_coverage;

        BEGIN;
        ALTER TABLE tasks RENAME TO tasks_v73_pre;
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'To Do',
            priority TEXT DEFAULT 'Medium',
            domain TEXT,
            assignee TEXT,
            task_type TEXT,
            priority_score INTEGER DEFAULT 0,
            expires_at TEXT,
            closed_reason TEXT,
            complexity TEXT,
            workflow TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            started_at TEXT,
            closed_at TEXT,
            merge_commit_sha TEXT,
            merge_base_sha TEXT,
            fixes_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
            bakeoff_id INTEGER,
            bakeoff_shadow INTEGER NOT NULL DEFAULT 0 CHECK (bakeoff_shadow IN (0, 1))
        );
        INSERT INTO tasks (
            id, summary, description, status, priority, domain, assignee,
            task_type, priority_score, expires_at, closed_reason, complexity,
            workflow, created_at, updated_at, started_at, closed_at,
            merge_commit_sha, merge_base_sha, fixes_task_id, bakeoff_id, bakeoff_shadow
        )
        SELECT
            id, summary, description, status, priority, domain, assignee,
            task_type, priority_score, expires_at, closed_reason, complexity,
            workflow, created_at, updated_at, started_at, closed_at,
            merge_commit_sha, merge_base_sha, fixes_task_id, bakeoff_id, bakeoff_shadow
        FROM tasks_v73_pre;
        DROP TABLE tasks_v73_pre;
        COMMIT;

        PRAGMA user_version = 72;
        """
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def db_at_v72_with_seed_tasks(db_at_v72):
    """v72 DB with two tasks: one references a file path (will be
    backfilled), one references nothing (no backfill rows)."""
    conn = sqlite3.connect(db_at_v72)
    conn.execute(
        "INSERT INTO tasks (id, summary, description, task_type, priority, complexity, priority_score) "
        "VALUES (9001, 'with-paths', 'Edit bin/tusk-foo.py and tests/integration/test_foo.py', 'feature', 'Medium', 'S', 10)"
    )
    conn.execute(
        "INSERT INTO acceptance_criteria (task_id, criterion, source, criterion_type, verification_spec) "
        "VALUES (9001, 'Verify the new helper', 'original', 'file', 'bin/tusk-foo.py')"
    )
    conn.execute(
        "INSERT INTO tasks (id, summary, description, task_type, priority, complexity, priority_score) "
        "VALUES (9002, 'no-paths', 'Rename a concept across the docs without naming any files.', 'feature', 'Medium', 'S', 10)"
    )
    conn.commit()
    conn.close()
    return db_at_v72


def _columns(db, table):
    conn = sqlite3.connect(db)
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    conn.close()
    return {r[1]: r for r in rows}


class TestMigrate73:

    def test_advances_schema_version_to_73(self, db_at_v72, config_path):
        assert tusk_migrate.get_version(db_at_v72) == 72
        tusk_migrate.migrate_73(db_at_v72, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db_at_v72) == 73

    def test_table_created_and_backfilled(
        self, db_at_v72_with_seed_tasks, config_path
    ):
        """task_scope table is created, and the task that references file
        paths in its description/criteria gets one auto_derived row per
        path. The task with no scope signal gets no rows."""
        tusk_migrate.migrate_73(db_at_v72_with_seed_tasks, config_path, SCRIPT_DIR)

        cols = _columns(db_at_v72_with_seed_tasks, "task_scope")
        assert set(cols.keys()) == {
            "id", "task_id", "pattern", "source", "reason",
            "locked_at", "locked_by", "created_at",
        }, f"unexpected task_scope columns: {sorted(cols.keys())}"

        conn = sqlite3.connect(db_at_v72_with_seed_tasks)
        conn.row_factory = sqlite3.Row
        rows_9001 = conn.execute(
            "SELECT pattern, source FROM task_scope WHERE task_id = 9001 ORDER BY pattern"
        ).fetchall()
        rows_9002 = conn.execute(
            "SELECT pattern, source FROM task_scope WHERE task_id = 9002"
        ).fetchall()
        conn.close()

        patterns_9001 = sorted(r["pattern"] for r in rows_9001)
        assert "bin/tusk-foo.py" in patterns_9001
        assert "tests/integration/test_foo.py" in patterns_9001
        assert all(r["source"] == "auto_derived" for r in rows_9001)
        assert rows_9002 == []

    def test_scope_enforced_defaults(self, db_at_v72, config_path):
        """Existing rows get scope_enforced=0 (legacy mode); a row inserted
        after migrate_73 picks up the DEFAULT 1 from the new column."""
        conn = sqlite3.connect(db_at_v72)
        conn.execute(
            "INSERT INTO tasks (id, summary, task_type, priority, complexity, priority_score) "
            "VALUES (9100, 'legacy', 'feature', 'Medium', 'S', 10)"
        )
        conn.commit()
        conn.close()

        cols_before = _columns(db_at_v72, "tasks")
        assert "scope_enforced" not in cols_before

        tusk_migrate.migrate_73(db_at_v72, config_path, SCRIPT_DIR)

        cols_after = _columns(db_at_v72, "tasks")
        assert "scope_enforced" in cols_after
        assert cols_after["scope_enforced"][2].upper() == "INTEGER"
        assert cols_after["scope_enforced"][3] == 1  # NOT NULL

        conn = sqlite3.connect(db_at_v72)
        legacy = conn.execute(
            "SELECT scope_enforced FROM tasks WHERE id = 9100"
        ).fetchone()[0]
        assert legacy == 0, "existing row must be backfilled to scope_enforced=0"

        conn.execute(
            "INSERT INTO tasks (id, summary, task_type, priority, complexity, priority_score) "
            "VALUES (9101, 'fresh', 'feature', 'Medium', 'S', 10)"
        )
        conn.commit()
        fresh = conn.execute(
            "SELECT scope_enforced FROM tasks WHERE id = 9101"
        ).fetchone()[0]
        conn.close()
        assert fresh == 1, "row inserted after migrate_73 must default to scope_enforced=1"

    def test_views_expose_scope_enforced(self, db_at_v72, config_path):
        """SELECT t.* views must be recreated so scope_enforced is reachable
        on a migrated DB (column-list freezing under ALTER TABLE)."""
        tusk_migrate.migrate_73(db_at_v72, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v72)
        # None of these may raise.
        conn.execute("SELECT scope_enforced FROM task_metrics LIMIT 1").fetchone()
        conn.execute("SELECT scope_enforced FROM v_ready_tasks LIMIT 1").fetchone()
        conn.execute("SELECT scope_enforced FROM v_chain_heads LIMIT 1").fetchone()
        conn.close()

    def test_idempotent_when_already_at_v73(self, db_path, config_path):
        """Fresh DB ships at v73+. Stamping explicitly keeps the test
        future-proof across later migrations; migrate_73 must short-circuit
        without touching the schema or bumping the version."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 73")
        conn.commit()
        version_before = tusk_migrate.get_version(str(db_path))
        rows_before = conn.execute(
            "SELECT COUNT(*) FROM task_scope"
        ).fetchone()[0]
        conn.close()

        tusk_migrate.migrate_73(str(db_path), config_path, SCRIPT_DIR)

        assert tusk_migrate.get_version(str(db_path)) == version_before
        conn = sqlite3.connect(str(db_path))
        rows_after = conn.execute(
            "SELECT COUNT(*) FROM task_scope"
        ).fetchone()[0]
        conn.close()
        assert rows_after == rows_before
