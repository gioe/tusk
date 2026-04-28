"""Integration test for migrate_59: filter deferred tasks out of v_ready_tasks
and v_chain_heads.

Covers:
- schema version advances 58 → 59
- A To Do task with ``is_deferred = 1`` does NOT appear in v_ready_tasks even
  when it has no blockers and would otherwise qualify — reproduces the bug
  where ``tusk task-start`` returned TASK-143 (deferred) as the next ready
  task.
- A non-Done task with ``is_deferred = 1`` does NOT appear in v_chain_heads
  even when it has unfinished downstream dependents.
- Non-deferred rows still surface normally (so the filter is narrow, not a
  regression).
- The two post-migration view definitions match a frozen v59-era snapshot
  (per CLAUDE.md, pin against a frozen snapshot — do not re-extract from
  live cmd_init — so later tasks-column migrations cannot silently drift
  this guard).
- Idempotent short-circuit on re-run against a fresh v59 install.
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
def db_at_v58_with_pre_v59_views(db_path):
    """Simulate a DB that was migrated up to v58 but has not yet run v59.

    Fresh installs ship v59+ and rebuild v_ready_tasks / v_chain_heads with
    the ``is_deferred`` filter in ``cmd_init``. To exercise the pre-v59
    shape, drop those two views and recreate them with the v58-era
    definitions (bakeoff_shadow filter present, is_deferred filter
    *absent*), then stamp user_version back to 58. Under that shape a
    deferred To Do task leaks into v_ready_tasks — exactly the regression
    migration 59 fixes.
    """
    db = str(db_path)
    conn = sqlite3.connect(db)
    # is_deferred was present at v58 but was later dropped in migration 63;
    # add it back so the migration's pre-v59 view shape and INSERTs against
    # tasks(is_deferred) work. ALTER TABLE ADD COLUMN does not interact with
    # existing views.
    conn.executescript(
        """
        ALTER TABLE tasks ADD COLUMN is_deferred INTEGER NOT NULL DEFAULT 0
            CHECK (is_deferred IN (0, 1));

        DROP VIEW IF EXISTS v_ready_tasks;
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

        DROP VIEW IF EXISTS v_chain_heads;
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
        """
    )
    conn.execute("PRAGMA user_version = 58")
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


def _insert_task(db, *, is_deferred=0, status="To Do", summary="t"):
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, status, priority, complexity, priority_score, is_deferred)"
            " VALUES (?, ?, 'Medium', 'S', 50, ?)",
            (summary, status, is_deferred),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _insert_dep(db, task_id, depends_on_id, relationship_type="blocks"):
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_id, relationship_type)"
            " VALUES (?, ?, ?)",
            (task_id, depends_on_id, relationship_type),
        )
        conn.commit()
    finally:
        conn.close()


# Frozen v59-era view definitions. Captured at the moment migration 59 landed
# so future tasks-column migrations that re-CREATE these views in cmd_init
# cannot silently drift this guard. Per CLAUDE.md / TASK-131, do not
# re-extract from live cmd_init.
_V59_VIEW_SQL = {
    "v_ready_tasks": """
        CREATE VIEW v_ready_tasks AS
        SELECT t.*
        FROM tasks t
        WHERE t.status = 'To Do'
          AND t.bakeoff_shadow = 0
          AND (t.is_deferred = 0 OR t.is_deferred IS NULL)
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
          AND (t.is_deferred = 0 OR t.is_deferred IS NULL)
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
}


class TestMigrate59:

    def test_advances_schema_version_to_59(self, db_at_v58_with_pre_v59_views, config_path):
        assert tusk_migrate.get_version(db_at_v58_with_pre_v59_views) == 58
        tusk_migrate.migrate_59(db_at_v58_with_pre_v59_views, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db_at_v58_with_pre_v59_views) >= 59

    def test_deferred_task_leaks_into_v_ready_tasks_before_migrate(
        self, db_at_v58_with_pre_v59_views
    ):
        """Baseline: pre-v59 views do NOT filter deferred — confirms the fixture
        reproduces the bug before migration runs."""
        deferred_id = _insert_task(db_at_v58_with_pre_v59_views, is_deferred=1)

        conn = sqlite3.connect(db_at_v58_with_pre_v59_views)
        try:
            ids = {r[0] for r in conn.execute("SELECT id FROM v_ready_tasks").fetchall()}
        finally:
            conn.close()

        assert deferred_id in ids, (
            "pre-v59 v_ready_tasks should leak deferred rows — if this assertion "
            "fails the fixture is wrong and the post-migrate assertion below is "
            "meaningless"
        )

    def test_deferred_task_excluded_from_v_ready_tasks_after_migrate(
        self, db_at_v58_with_pre_v59_views, config_path
    ):
        deferred_id = _insert_task(db_at_v58_with_pre_v59_views, is_deferred=1)
        non_deferred_id = _insert_task(db_at_v58_with_pre_v59_views, is_deferred=0)

        tusk_migrate.migrate_59(db_at_v58_with_pre_v59_views, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v58_with_pre_v59_views)
        try:
            ids = {r[0] for r in conn.execute("SELECT id FROM v_ready_tasks").fetchall()}
        finally:
            conn.close()

        assert deferred_id not in ids, "deferred task must not surface as ready-to-work"
        assert non_deferred_id in ids, "non-deferred task must still surface"

    def test_deferred_task_excluded_from_v_chain_heads_after_migrate(
        self, db_at_v58_with_pre_v59_views, config_path
    ):
        # Chain-head qualifying shape: a downstream task depends on the head,
        # downstream is non-Done, and the head itself has no upstream
        # blockers. A deferred head meeting these conditions must still be
        # filtered out post-migration.
        deferred_head = _insert_task(db_at_v58_with_pre_v59_views, is_deferred=1)
        downstream = _insert_task(db_at_v58_with_pre_v59_views, is_deferred=0)
        _insert_dep(db_at_v58_with_pre_v59_views, downstream, deferred_head)

        non_deferred_head = _insert_task(db_at_v58_with_pre_v59_views, is_deferred=0)
        downstream2 = _insert_task(db_at_v58_with_pre_v59_views, is_deferred=0)
        _insert_dep(db_at_v58_with_pre_v59_views, downstream2, non_deferred_head)

        tusk_migrate.migrate_59(db_at_v58_with_pre_v59_views, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v58_with_pre_v59_views)
        try:
            ids = {r[0] for r in conn.execute("SELECT id FROM v_chain_heads").fetchall()}
        finally:
            conn.close()

        assert deferred_head not in ids, "deferred task must not surface as chain head"
        assert non_deferred_head in ids, "non-deferred chain head must still surface"

    def test_view_definitions_match_frozen_v59_snapshot(
        self, db_at_v58_with_pre_v59_views, config_path
    ):
        tusk_migrate.migrate_59(db_at_v58_with_pre_v59_views, config_path, SCRIPT_DIR)

        def _normalize(sql):
            return re.sub(r"\s+", " ", sql).strip()

        for view, expected in _V59_VIEW_SQL.items():
            db_sql = _view_sql(db_at_v58_with_pre_v59_views, view)
            assert db_sql is not None, f"view {view} missing after migrate_59"
            assert _normalize(db_sql) == _normalize(expected), (
                f"view {view} does not match frozen v59 snapshot"
            )

    def test_idempotent_when_already_at_v59(self, db_path, config_path):
        """Fresh DB ships v59+; re-running migrate_59 is a no-op short-circuit."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 59")
        conn.commit()
        conn.close()

        version_before = tusk_migrate.get_version(str(db_path))
        tusk_migrate.migrate_59(str(db_path), config_path, SCRIPT_DIR)

        assert tusk_migrate.get_version(str(db_path)) == version_before
