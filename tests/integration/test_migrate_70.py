"""Integration test for migrate_70: add tasks.merge_commit_sha and recreate
tasks-dependent views so the new column propagates into ``task_metrics``,
``v_ready_tasks``, and ``v_chain_heads`` on DBs upgraded from v69.

Covers:
- schema version advances 69 → 70
- ``tasks.merge_commit_sha TEXT`` is added (nullable, existing rows stay NULL)
- ``task_metrics``, ``v_ready_tasks``, ``v_chain_heads`` are recreated with
  the current tasks.* column list so SELECT merge_commit_sha succeeds —
  the canonical regression a tasks-column migration must close (TASK-131
  guard against view column-list freezing under ALTER TABLE)
- the four view definitions match a frozen v70-era snapshot
- ``v_criteria_coverage``'s projected columns are unchanged (it never
  projected t.*, so column-list freezing never affected it; the migration
  still DROP+CREATEs it for uniformity)
- idempotent short-circuit on re-run against a fresh v70 install

The v70 view shape carries ``WHERE t.bakeoff_shadow = 0`` introduced by
migration 58 — the snapshot is pinned, not re-extracted from live
``cmd_init``, so future tasks-column migrations that further alter these
views cannot retroactively break this guard (TASK-131).
"""

import importlib.util
import os
import re
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
def db_at_v69_with_pre_v69_views(db_path):
    """Simulate a DB that was migrated from v68 → v69.

    Fresh installs ship v70+ and rebuild views end-to-end in ``cmd_init``,
    so ``merge_commit_sha`` is already present in ``task_metrics``. To
    reproduce the migrated-DB trap, drop the column from the live tasks
    table (via the SQLite 12-step ALTER TABLE pattern is overkill — we
    just recreate the table and views with the pre-v70 column list), then
    stamp the DB back to version 69. Under that shape, SELECT
    merge_commit_sha FROM task_metrics fails with ``no such column`` —
    the regression migration 70 fixes.
    """
    db = str(db_path)
    conn = sqlite3.connect(db)

    # Drop the merge_commit_sha column from tasks first (SQLite 12-step
    # ALTER TABLE pattern: rename, recreate without the column, copy,
    # drop). The DROP VIEW IF EXISTS lines run before the rename so views
    # don't pin a stale schema_internal_id to tasks_v70_pre; the recreate
    # below rebuilds them against the new tasks table with the pre-v70
    # column projection.
    conn.executescript(
        """
        DROP VIEW IF EXISTS task_metrics;
        DROP VIEW IF EXISTS v_ready_tasks;
        DROP VIEW IF EXISTS v_chain_heads;
        DROP VIEW IF EXISTS v_criteria_coverage;
        DROP VIEW IF EXISTS v_blocked_tasks;
        DROP VIEW IF EXISTS v_velocity;

        BEGIN;
        ALTER TABLE tasks RENAME TO tasks_v70_pre;
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
            fixes_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
            bakeoff_id INTEGER,
            bakeoff_shadow INTEGER NOT NULL DEFAULT 0 CHECK (bakeoff_shadow IN (0, 1))
        );
        INSERT INTO tasks (
            id, summary, description, status, priority, domain, assignee,
            task_type, priority_score, expires_at, closed_reason, complexity,
            workflow, created_at, updated_at, started_at, closed_at,
            fixes_task_id, bakeoff_id, bakeoff_shadow
        )
        SELECT
            id, summary, description, status, priority, domain, assignee,
            task_type, priority_score, expires_at, closed_reason, complexity,
            workflow, created_at, updated_at, started_at, closed_at,
            fixes_task_id, bakeoff_id, bakeoff_shadow
        FROM tasks_v70_pre;
        DROP TABLE tasks_v70_pre;
        COMMIT;
        """
    )

    # Now recreate the views against the pre-v70 tasks table. Pre-v69
    # views projected SELECT t.* and froze the column list at CREATE time,
    # so under v69's shape they have every tasks column EXCEPT
    # merge_commit_sha. SELECT merge_commit_sha FROM task_metrics will
    # then fail with "no such column" — exactly the regression migrate_70
    # closes.
    cols = [
        r[1]
        for r in conn.execute("PRAGMA table_info(tasks)").fetchall()
    ]
    projection = ", ".join(f't."{c}"' for c in cols)

    conn.executescript(
        f"""
        CREATE VIEW task_metrics AS
        SELECT {projection},
            COUNT(s.id) as session_count,
            SUM(s.duration_seconds) as total_duration_seconds,
            SUM(s.cost_dollars) as total_cost,
            SUM(s.tokens_in) as total_tokens_in,
            SUM(s.tokens_out) as total_tokens_out,
            SUM(s.lines_added) as total_lines_added,
            SUM(s.lines_removed) as total_lines_removed,
            SUM(s.request_count) as total_request_count,
            (SELECT COUNT(*) FROM task_status_transitions tst
              WHERE tst.task_id = t.id AND tst.to_status = 'To Do') as reopen_count
        FROM tasks t
        LEFT JOIN task_sessions s ON t.id = s.task_id
        WHERE t.bakeoff_shadow = 0
        GROUP BY t.id;

        CREATE VIEW v_ready_tasks AS
        SELECT {projection}
        FROM tasks t
        WHERE t.status = 'To Do'
          AND t.bakeoff_shadow = 0
          AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on_id = blocker.id
            WHERE d.task_id = t.id AND d.relationship_type = 'blocks' AND blocker.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM external_blockers eb
            WHERE eb.task_id = t.id AND eb.is_resolved = 0
          );

        CREATE VIEW v_chain_heads AS
        SELECT {projection}
        FROM tasks t
        WHERE t.status <> 'Done'
          AND t.bakeoff_shadow = 0
          AND EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks downstream ON d.task_id = downstream.id
            WHERE d.depends_on_id = t.id AND downstream.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on_id = blocker.id
            WHERE d.task_id = t.id AND d.relationship_type = 'blocks' AND blocker.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM external_blockers eb
            WHERE eb.task_id = t.id AND eb.is_resolved = 0
          );
        """
    )

    conn.execute("PRAGMA user_version = 69")
    conn.commit()
    conn.close()
    return db


def _view_sql(db, name):
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name=?", (name,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


# Frozen v70-era view definitions. These capture the shape fresh installs
# had immediately after migration 70 landed. Pinning to a snapshot (rather
# than live ``bin/tusk``) keeps this guard stable across future
# tasks-column migrations: a migration N test must verify the
# post-migrate_N state matches what fresh v(N) installs had, not what
# fresh v(latest) installs have today (TASK-131).
_V70_VIEW_SQL = {
    "task_metrics": """
        CREATE VIEW task_metrics AS
        SELECT t.*,
            COUNT(s.id) as session_count,
            SUM(s.duration_seconds) as total_duration_seconds,
            SUM(s.cost_dollars) as total_cost,
            SUM(s.tokens_in) as total_tokens_in,
            SUM(s.tokens_out) as total_tokens_out,
            SUM(s.lines_added) as total_lines_added,
            SUM(s.lines_removed) as total_lines_removed,
            SUM(s.request_count) as total_request_count,
            (SELECT COUNT(*) FROM task_status_transitions tst
              WHERE tst.task_id = t.id AND tst.to_status = 'To Do') as reopen_count
        FROM tasks t
        LEFT JOIN task_sessions s ON t.id = s.task_id
        WHERE t.bakeoff_shadow = 0
        GROUP BY t.id
    """,
    "v_ready_tasks": """
        CREATE VIEW v_ready_tasks AS
        SELECT t.*
        FROM tasks t
        WHERE t.status = 'To Do'
          AND t.bakeoff_shadow = 0
          AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on_id = blocker.id
            WHERE d.task_id = t.id AND d.relationship_type = 'blocks' AND blocker.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM external_blockers eb
            WHERE eb.task_id = t.id AND eb.is_resolved = 0
          )
    """,
    "v_chain_heads": """
        CREATE VIEW v_chain_heads AS
        SELECT t.*
        FROM tasks t
        WHERE t.status <> 'Done'
          AND t.bakeoff_shadow = 0
          AND EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks downstream ON d.task_id = downstream.id
            WHERE d.depends_on_id = t.id AND downstream.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on_id = blocker.id
            WHERE d.task_id = t.id AND d.relationship_type = 'blocks' AND blocker.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM external_blockers eb
            WHERE eb.task_id = t.id AND eb.is_resolved = 0
          )
    """,
    "v_criteria_coverage": """
        CREATE VIEW v_criteria_coverage AS
        SELECT t.id AS task_id,
               t.summary,
               COUNT(CASE WHEN ac.is_deferred = 0 OR ac.is_deferred IS NULL THEN 1 END) AS total_criteria,
               COALESCE(SUM(CASE WHEN ac.is_completed = 1 AND (ac.is_deferred = 0 OR ac.is_deferred IS NULL) THEN 1 ELSE 0 END), 0) AS completed_criteria,
               COUNT(CASE WHEN ac.is_deferred = 0 OR ac.is_deferred IS NULL THEN 1 END) - COALESCE(SUM(CASE WHEN ac.is_completed = 1 AND (ac.is_deferred = 0 OR ac.is_deferred IS NULL) THEN 1 ELSE 0 END), 0) AS remaining_criteria
        FROM tasks t
        LEFT JOIN acceptance_criteria ac ON ac.task_id = t.id
        WHERE t.bakeoff_shadow = 0
        GROUP BY t.id, t.summary
    """,
}


class TestMigrate70:

    def test_advances_schema_version_to_70(
        self, db_at_v69_with_pre_v69_views, config_path
    ):
        assert tusk_migrate.get_version(db_at_v69_with_pre_v69_views) == 69
        tusk_migrate.migrate_70(db_at_v69_with_pre_v69_views, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db_at_v69_with_pre_v69_views) == 70

    def test_adds_merge_commit_sha_column(
        self, db_at_v69_with_pre_v69_views, config_path
    ):
        """tasks.merge_commit_sha must be present after migrate_70 — nullable
        TEXT, with existing rows untouched (NULL)."""
        conn = sqlite3.connect(db_at_v69_with_pre_v69_views)
        cols_before = [
            r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()
        ]
        assert "merge_commit_sha" not in cols_before
        conn.close()

        tusk_migrate.migrate_70(db_at_v69_with_pre_v69_views, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v69_with_pre_v69_views)
        col_info = {
            r[1]: r
            for r in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        assert "merge_commit_sha" in col_info
        # col_info[name] = (cid, name, type, notnull, dflt_value, pk)
        assert col_info["merge_commit_sha"][2].upper() == "TEXT"
        assert col_info["merge_commit_sha"][3] == 0  # nullable
        conn.close()

    def test_task_metrics_exposes_merge_commit_sha_after_migrate(
        self, db_at_v69_with_pre_v69_views, config_path
    ):
        """On a DB migrated from v68, SELECT merge_commit_sha FROM
        task_metrics must fail before migrate_70 and succeed after — the
        canonical column-list-freeze regression a tasks-column migration
        must close."""
        conn = sqlite3.connect(db_at_v69_with_pre_v69_views)
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            conn.execute("SELECT merge_commit_sha FROM task_metrics LIMIT 1").fetchone()
        conn.close()

        tusk_migrate.migrate_70(db_at_v69_with_pre_v69_views, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v69_with_pre_v69_views)
        # Must not raise.
        conn.execute("SELECT merge_commit_sha FROM task_metrics LIMIT 1").fetchone()
        conn.close()

    def test_v_ready_tasks_and_v_chain_heads_expose_merge_commit_sha(
        self, db_at_v69_with_pre_v69_views, config_path
    ):
        """The other two SELECT t.* views are recreated in the same pass."""
        tusk_migrate.migrate_70(db_at_v69_with_pre_v69_views, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v69_with_pre_v69_views)
        # Neither must raise.
        conn.execute("SELECT merge_commit_sha FROM v_ready_tasks LIMIT 1").fetchone()
        conn.execute("SELECT merge_commit_sha FROM v_chain_heads LIMIT 1").fetchone()
        conn.close()

    def test_v_criteria_coverage_projects_unchanged_columns(
        self, db_at_v69_with_pre_v69_views, config_path
    ):
        """v_criteria_coverage never projected t.*, so its column list does
        not freeze against tasks ALTER TABLE. The migration still DROPs
        and re-CREATEs it for uniformity; the resulting columns must
        remain task_id, summary, total_criteria, completed_criteria,
        remaining_criteria."""
        tusk_migrate.migrate_70(db_at_v69_with_pre_v69_views, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v69_with_pre_v69_views)
        cols = [
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(v_criteria_coverage)"
            ).fetchall()
        ]
        conn.close()

        assert cols == [
            "task_id",
            "summary",
            "total_criteria",
            "completed_criteria",
            "remaining_criteria",
        ]

    def test_view_definitions_match_v70_snapshot(
        self, db_at_v69_with_pre_v69_views, config_path
    ):
        """Each recreated view's stored SQL must match its frozen v70-era
        snapshot (whitespace-normalized). The snapshot is pinned rather
        than re-extracted from live cmd_init: future tasks-column
        migrations may further alter these views, and those belong to
        their own migration-N shapes, not migrate_70's v70 shape."""
        tusk_migrate.migrate_70(db_at_v69_with_pre_v69_views, config_path, SCRIPT_DIR)

        def _normalize(sql):
            return re.sub(r"\s+", " ", sql).strip().rstrip(";")

        for view, expected in _V70_VIEW_SQL.items():
            db_sql = _view_sql(db_at_v69_with_pre_v69_views, view)
            assert db_sql is not None, f"{view} missing after migrate_70"
            assert _normalize(db_sql) == _normalize(expected), (
                f"{view} definition drifted from v70 snapshot"
            )

    def test_idempotent_when_already_at_v70(self, db_path, config_path):
        """Fresh DB ships at v70+. Stamping to 70 explicitly keeps the test
        future-proof across later migrations; migrate_70 must short-circuit
        without touching any view or bumping the version."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 70")
        conn.commit()
        conn.close()

        before = _view_sql(str(db_path), "task_metrics")
        version_before = tusk_migrate.get_version(str(db_path))

        tusk_migrate.migrate_70(str(db_path), config_path, SCRIPT_DIR)

        assert tusk_migrate.get_version(str(db_path)) == version_before
        assert _view_sql(str(db_path), "task_metrics") == before
