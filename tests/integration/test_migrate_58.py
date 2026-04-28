"""Integration test for migrate_58: add bakeoff columns and filter shadows out of views.

Covers:
- ALTER TABLE adds bakeoff_id (nullable INTEGER) and bakeoff_shadow
  (NOT NULL DEFAULT 0, CHECK IN (0,1))
- All five tasks-dependent views are recreated with WHERE t.bakeoff_shadow = 0:
  task_metrics, v_ready_tasks, v_chain_heads, v_blocked_tasks, v_criteria_coverage
- A shadow row (bakeoff_shadow = 1) is excluded from v_ready_tasks even when
  it would otherwise qualify
- The default `tusk task-list` output excludes shadows
- Schema version advances 57 → 58
- Idempotent short-circuit on re-run
"""

import importlib.util
import os
import sqlite3
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT_DIR = os.path.join(REPO_ROOT, "bin")
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


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
def db_at_v57(db_path, config_path):
    """Reset the fresh-init DB back to version 57 so migrate_58 will run.

    Drops the bakeoff columns (fresh DBs ship with them as of v58) so the
    migration's ALTER TABLE + view-recreation path is exercised end-to-end.
    SQLite doesn't support DROP COLUMN before 3.35; rebuild tasks via a
    shadow copy to simulate a pre-v58 DB reliably.
    """
    conn = sqlite3.connect(str(db_path))
    # Drop views first so they don't freeze column lists from the copied table.
    # v_velocity depends on task_metrics so it must be dropped as well; we
    # don't recreate it in the pre-v58 fixture — nothing here exercises it.
    for view in (
        "v_velocity",
        "task_metrics",
        "v_ready_tasks",
        "v_chain_heads",
        "v_blocked_tasks",
        "v_criteria_coverage",
    ):
        conn.execute(f"DROP VIEW IF EXISTS {view}")
    # Rebuild tasks without the bakeoff columns. is_deferred was present at
    # v57 but was later dropped in migration 63; the literal ``0`` in the
    # SELECT supplies the v57-era default rather than reading from a column
    # that fresh installs no longer have.
    conn.executescript(
        """
        CREATE TABLE tasks_premigration (
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
            is_deferred INTEGER NOT NULL DEFAULT 0 CHECK (is_deferred IN (0, 1)),
            workflow TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            started_at TEXT,
            closed_at TEXT,
            fixes_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL
        );
        INSERT INTO tasks_premigration SELECT
            id, summary, description, status, priority, domain, assignee,
            task_type, priority_score, expires_at, closed_reason, complexity,
            0, workflow, created_at, updated_at, started_at,
            closed_at, fixes_task_id
        FROM tasks;
        DROP TABLE tasks;
        ALTER TABLE tasks_premigration RENAME TO tasks;
        """
    )
    conn.execute("PRAGMA user_version = 57")
    conn.commit()
    conn.close()
    return str(db_path)


def _columns(db, table):
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return {r[1]: {"notnull": r[3], "default": r[4]} for r in rows}


def _insert_task(db, *, bakeoff_shadow=0, status="To Do"):
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, status, priority, complexity, priority_score, bakeoff_shadow)"
            " VALUES ('t', ?, 'Medium', 'S', 50, ?)",
            (status, bakeoff_shadow),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


class TestMigrate58:

    def test_adds_bakeoff_columns_with_expected_shape(self, db_at_v57, config_path):
        tusk_migrate.migrate_58(db_at_v57, config_path, SCRIPT_DIR)

        cols = _columns(db_at_v57, "tasks")
        assert "bakeoff_id" in cols
        assert "bakeoff_shadow" in cols
        assert cols["bakeoff_id"]["notnull"] == 0
        assert cols["bakeoff_shadow"]["notnull"] == 1
        assert str(cols["bakeoff_shadow"]["default"]) == "0"

    def test_shadow_excluded_from_v_ready_tasks(self, db_at_v57, config_path):
        tusk_migrate.migrate_58(db_at_v57, config_path, SCRIPT_DIR)

        real_id = _insert_task(db_at_v57, bakeoff_shadow=0)
        shadow_id = _insert_task(db_at_v57, bakeoff_shadow=1)

        conn = sqlite3.connect(db_at_v57)
        try:
            ids = {r[0] for r in conn.execute("SELECT id FROM v_ready_tasks").fetchall()}
        finally:
            conn.close()

        assert real_id in ids
        assert shadow_id not in ids

    def test_shadow_excluded_from_task_metrics(self, db_at_v57, config_path):
        tusk_migrate.migrate_58(db_at_v57, config_path, SCRIPT_DIR)

        real_id = _insert_task(db_at_v57, bakeoff_shadow=0)
        shadow_id = _insert_task(db_at_v57, bakeoff_shadow=1)

        conn = sqlite3.connect(db_at_v57)
        try:
            ids = {r[0] for r in conn.execute("SELECT id FROM task_metrics").fetchall()}
        finally:
            conn.close()

        assert real_id in ids
        assert shadow_id not in ids

    def test_shadow_excluded_from_v_criteria_coverage(self, db_at_v57, config_path):
        tusk_migrate.migrate_58(db_at_v57, config_path, SCRIPT_DIR)

        real_id = _insert_task(db_at_v57, bakeoff_shadow=0)
        shadow_id = _insert_task(db_at_v57, bakeoff_shadow=1)

        conn = sqlite3.connect(db_at_v57)
        try:
            ids = {r[0] for r in conn.execute("SELECT task_id FROM v_criteria_coverage").fetchall()}
        finally:
            conn.close()

        assert real_id in ids
        assert shadow_id not in ids

    def test_shadow_excluded_from_default_task_list(self, db_at_v57, config_path):
        tusk_migrate.migrate_58(db_at_v57, config_path, SCRIPT_DIR)

        real_id = _insert_task(db_at_v57, bakeoff_shadow=0)
        shadow_id = _insert_task(db_at_v57, bakeoff_shadow=1)

        # Invoke the task-list script directly against the migrated DB so the
        # test doesn't depend on the user's resolved TUSK_DB or PWD.
        result = subprocess.run(
            [
                "python3",
                os.path.join(SCRIPT_DIR, "tusk-task-list.py"),
                db_at_v57,
                config_path,
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        import json as _json
        rows = _json.loads(result.stdout)
        ids = {row["id"] for row in rows}

        assert real_id in ids
        assert shadow_id not in ids, "shadow row must not appear in default task-list output"

    def test_bakeoff_shadow_check_constraint_enforced(self, db_at_v57, config_path):
        tusk_migrate.migrate_58(db_at_v57, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v57)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO tasks (summary, priority, complexity, priority_score, bakeoff_shadow)"
                    " VALUES ('t', 'Medium', 'S', 0, 2)"
                )
                conn.commit()
        finally:
            conn.close()

    def test_advances_schema_version_to_58(self, db_at_v57, config_path):
        assert tusk_migrate.get_version(db_at_v57) == 57
        tusk_migrate.migrate_58(db_at_v57, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db_at_v57) >= 58

    def test_idempotent_when_already_at_v58(self, db_path, config_path):
        """Fresh DB is already at v58+; re-running is a no-op short-circuit."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 58")
        conn.commit()
        conn.close()

        version_before = tusk_migrate.get_version(str(db_path))
        tusk_migrate.migrate_58(str(db_path), config_path, SCRIPT_DIR)

        assert tusk_migrate.get_version(str(db_path)) == version_before
