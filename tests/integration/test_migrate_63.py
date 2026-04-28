"""Integration test for migrate_63: drop tasks.is_deferred + recreate views.

Covers:
- schema version advances 62 -> 63
- tasks.is_deferred column is removed
- pre-existing rows survive (is_deferred=1 rows are NOT deleted; they simply
  lose the column)
- task_metrics, v_ready_tasks, v_chain_heads no longer expose is_deferred
- the four recreated view definitions match a frozen v63-era snapshot
- v_criteria_coverage's projected columns are unchanged
- idempotent short-circuit on re-run against a fresh v63 install
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
def db_at_v62_with_is_deferred(db_path):
    """Reconstitute a DB shaped like v62: tasks has is_deferred, the three
    SELECT t.* views project it, and v_criteria_coverage references
    ac.is_deferred (criterion-level — unaffected). Stamp PRAGMA user_version=62.

    Fresh installs ship at v63+ (cmd_init has the column dropped already), so
    we have to put is_deferred back to reproduce the migration target.
    """
    db = str(db_path)
    conn = sqlite3.connect(db)

    conn.executescript(
        """
        DROP VIEW IF EXISTS task_metrics;
        DROP VIEW IF EXISTS v_ready_tasks;
        DROP VIEW IF EXISTS v_chain_heads;
        DROP VIEW IF EXISTS v_criteria_coverage;

        ALTER TABLE tasks ADD COLUMN is_deferred INTEGER NOT NULL DEFAULT 0
            CHECK (is_deferred IN (0, 1));

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
        GROUP BY t.id;

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
          );

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
          );

        CREATE VIEW v_criteria_coverage AS
        SELECT t.id AS task_id,
               t.summary,
               COUNT(CASE WHEN ac.is_deferred = 0 OR ac.is_deferred IS NULL THEN 1 END) AS total_criteria,
               COALESCE(SUM(CASE WHEN ac.is_completed = 1 AND (ac.is_deferred = 0 OR ac.is_deferred IS NULL) THEN 1 ELSE 0 END), 0) AS completed_criteria,
               COUNT(CASE WHEN ac.is_deferred = 0 OR ac.is_deferred IS NULL THEN 1 END) - COALESCE(SUM(CASE WHEN ac.is_completed = 1 AND (ac.is_deferred = 0 OR ac.is_deferred IS NULL) THEN 1 ELSE 0 END), 0) AS remaining_criteria
        FROM tasks t
        LEFT JOIN acceptance_criteria ac ON ac.task_id = t.id
        WHERE t.bakeoff_shadow = 0
        GROUP BY t.id, t.summary;

        PRAGMA user_version = 62;
        """
    )
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


# Frozen v63-era view definitions: the shape fresh installs have immediately
# after migration 63 lands. Pinning to a snapshot (rather than re-extracting
# from live cmd_init) keeps this guard stable across future tasks-column
# migrations that further alter cmd_init's views — see CLAUDE.md.
_V63_VIEW_SQL = {
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


class TestMigrate63:

    def test_advances_schema_version_to_63(
        self, db_at_v62_with_is_deferred, config_path
    ):
        assert tusk_migrate.get_version(db_at_v62_with_is_deferred) == 62
        tusk_migrate.migrate_63(db_at_v62_with_is_deferred, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db_at_v62_with_is_deferred) == 63

    def test_drops_is_deferred_column_from_tasks(
        self, db_at_v62_with_is_deferred, config_path
    ):
        conn = sqlite3.connect(db_at_v62_with_is_deferred)
        before_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        conn.close()
        assert "is_deferred" in before_cols

        tusk_migrate.migrate_63(db_at_v62_with_is_deferred, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v62_with_is_deferred)
        after_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        conn.close()
        assert "is_deferred" not in after_cols

    def test_preserves_rows_with_is_deferred_set(
        self, db_at_v62_with_is_deferred, config_path
    ):
        """is_deferred=1 rows must survive — the column drops, the row stays."""
        conn = sqlite3.connect(db_at_v62_with_is_deferred)
        conn.execute(
            "INSERT INTO tasks (summary, status, is_deferred) "
            "VALUES ('legacy deferred task', 'To Do', 1)"
        )
        conn.commit()
        row_id = conn.execute(
            "SELECT id FROM tasks WHERE summary = 'legacy deferred task'"
        ).fetchone()[0]
        conn.close()

        tusk_migrate.migrate_63(db_at_v62_with_is_deferred, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v62_with_is_deferred)
        survivor = conn.execute(
            "SELECT summary, status FROM tasks WHERE id = ?", (row_id,)
        ).fetchone()
        conn.close()
        assert survivor == ("legacy deferred task", "To Do")

    def test_views_no_longer_expose_is_deferred(
        self, db_at_v62_with_is_deferred, config_path
    ):
        """task_metrics, v_ready_tasks, v_chain_heads previously projected
        is_deferred via SELECT t.*. After migration, querying it must fail."""
        tusk_migrate.migrate_63(db_at_v62_with_is_deferred, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v62_with_is_deferred)
        for view in ("task_metrics", "v_ready_tasks", "v_chain_heads"):
            with pytest.raises(sqlite3.OperationalError, match="no such column"):
                conn.execute(f"SELECT is_deferred FROM {view} LIMIT 1").fetchone()
        conn.close()

    def test_v_criteria_coverage_projects_unchanged_columns(
        self, db_at_v62_with_is_deferred, config_path
    ):
        """v_criteria_coverage never projected t.* and continues to filter on
        ac.is_deferred (criterion-level, separate concept). Its column list
        stays: task_id, summary, total_criteria, completed_criteria,
        remaining_criteria."""
        tusk_migrate.migrate_63(db_at_v62_with_is_deferred, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v62_with_is_deferred)
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

    def test_view_definitions_match_v63_snapshot(
        self, db_at_v62_with_is_deferred, config_path
    ):
        """Each recreated view's stored SQL must match its frozen v63-era
        snapshot (whitespace-normalized)."""
        tusk_migrate.migrate_63(db_at_v62_with_is_deferred, config_path, SCRIPT_DIR)

        def _normalize(sql):
            return re.sub(r"\s+", " ", sql).strip().rstrip(";")

        for view, expected in _V63_VIEW_SQL.items():
            db_sql = _view_sql(db_at_v62_with_is_deferred, view)
            assert db_sql is not None, f"{view} missing after migrate_63"
            assert _normalize(db_sql) == _normalize(expected), (
                f"{view} definition drifted from v63 snapshot"
            )

    def test_idempotent_when_already_at_v63(self, db_path, config_path):
        """Fresh DB ships at v63+. Stamping to 63 explicitly keeps the test
        future-proof across later migrations; migrate_63 must short-circuit
        without touching any view or bumping the version."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 63")
        conn.commit()
        conn.close()

        before = _view_sql(str(db_path), "task_metrics")
        version_before = tusk_migrate.get_version(str(db_path))

        tusk_migrate.migrate_63(str(db_path), config_path, SCRIPT_DIR)

        assert tusk_migrate.get_version(str(db_path)) == version_before
        assert _view_sql(str(db_path), "task_metrics") == before
