"""Integration test for migrate_61: stop filtering deferred tasks out of
v_ready_tasks and v_chain_heads (Issue #584).

Reverses migration 59. Migration 59 added
``(is_deferred = 0 OR is_deferred IS NULL)`` to both views, which created a
hidden third state — deferred tasks were ``status='To Do'`` but invisible
to ``/tusk``, ``/tusk blocked``, and ``/loop`` while still appearing in raw
SELECTs on the tasks table. Migration 61 removes that filter so deferred
tasks compete on WSJF score like any other To Do task.

Covers:
- schema version advances 60 → 61
- A To Do task with ``is_deferred = 1`` DOES appear in v_ready_tasks after
  migration when it has no blockers — reverses the v59 filter behavior.
- A non-Done task with ``is_deferred = 1`` DOES appear in v_chain_heads
  after migration when it has unfinished downstream dependents.
- Non-deferred rows still surface (no regression on the non-deferred path).
- The two post-migration view definitions match a frozen v61-era snapshot
  (per CLAUDE.md, pin against a frozen snapshot — do not re-extract from
  live cmd_init — so later tasks-column migrations cannot silently drift
  this guard).
- Idempotent short-circuit on re-run against a fresh v61 install.
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
def db_at_v60_with_v59_views(db_path):
    """Simulate a DB that was migrated up to v60 but has not yet run v61.

    Fresh installs ship v61+ and rebuild v_ready_tasks / v_chain_heads
    *without* the ``is_deferred`` filter in ``cmd_init``. To exercise the
    pre-v61 shape, drop those two views and recreate them with the v59-era
    definitions (filter present), then stamp user_version back to 60. Under
    that shape a deferred To Do task is hidden from v_ready_tasks — exactly
    the regression migration 61 reverses.
    """
    db = str(db_path)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        DROP VIEW IF EXISTS v_ready_tasks;
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
          );

        DROP VIEW IF EXISTS v_chain_heads;
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
          );
        """
    )
    conn.execute("PRAGMA user_version = 60")
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


# Frozen v61-era view definitions. Captured at the moment migration 61 landed
# so future tasks-column migrations that re-CREATE these views in cmd_init
# cannot silently drift this guard. Per CLAUDE.md / TASK-131, do not
# re-extract from live cmd_init.
_V61_VIEW_SQL = {
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
}


class TestMigrate61:

    def test_advances_schema_version_to_61(self, db_at_v60_with_v59_views, config_path):
        assert tusk_migrate.get_version(db_at_v60_with_v59_views) == 60
        tusk_migrate.migrate_61(db_at_v60_with_v59_views, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db_at_v60_with_v59_views) >= 61

    def test_deferred_task_hidden_from_v_ready_tasks_before_migrate(
        self, db_at_v60_with_v59_views
    ):
        """Baseline: v59-shaped views filter deferred — confirms the fixture
        reproduces the bug before migration runs."""
        deferred_id = _insert_task(db_at_v60_with_v59_views, is_deferred=1)

        conn = sqlite3.connect(db_at_v60_with_v59_views)
        try:
            ids = {r[0] for r in conn.execute("SELECT id FROM v_ready_tasks").fetchall()}
        finally:
            conn.close()

        assert deferred_id not in ids, (
            "v59-shaped v_ready_tasks should hide deferred rows — if this assertion "
            "fails the fixture is wrong and the post-migrate assertion below is "
            "meaningless"
        )

    def test_deferred_task_surfaces_in_v_ready_tasks_after_migrate(
        self, db_at_v60_with_v59_views, config_path
    ):
        deferred_id = _insert_task(db_at_v60_with_v59_views, is_deferred=1)
        non_deferred_id = _insert_task(db_at_v60_with_v59_views, is_deferred=0)

        tusk_migrate.migrate_61(db_at_v60_with_v59_views, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v60_with_v59_views)
        try:
            ids = {r[0] for r in conn.execute("SELECT id FROM v_ready_tasks").fetchall()}
        finally:
            conn.close()

        assert deferred_id in ids, "deferred To Do task must surface as ready-to-work post-v61"
        assert non_deferred_id in ids, "non-deferred task must still surface"

    def test_deferred_task_surfaces_in_v_chain_heads_after_migrate(
        self, db_at_v60_with_v59_views, config_path
    ):
        # Chain-head qualifying shape: a downstream task depends on the head,
        # downstream is non-Done, and the head itself has no upstream
        # blockers. A deferred head meeting these conditions must surface
        # post-migration.
        deferred_head = _insert_task(db_at_v60_with_v59_views, is_deferred=1)
        downstream = _insert_task(db_at_v60_with_v59_views, is_deferred=0)
        _insert_dep(db_at_v60_with_v59_views, downstream, deferred_head)

        non_deferred_head = _insert_task(db_at_v60_with_v59_views, is_deferred=0)
        downstream2 = _insert_task(db_at_v60_with_v59_views, is_deferred=0)
        _insert_dep(db_at_v60_with_v59_views, downstream2, non_deferred_head)

        tusk_migrate.migrate_61(db_at_v60_with_v59_views, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v60_with_v59_views)
        try:
            ids = {r[0] for r in conn.execute("SELECT id FROM v_chain_heads").fetchall()}
        finally:
            conn.close()

        assert deferred_head in ids, "deferred chain head must surface post-v61"
        assert non_deferred_head in ids, "non-deferred chain head must still surface"

    def test_view_definitions_match_frozen_v61_snapshot(
        self, db_at_v60_with_v59_views, config_path
    ):
        tusk_migrate.migrate_61(db_at_v60_with_v59_views, config_path, SCRIPT_DIR)

        def _normalize(sql):
            return re.sub(r"\s+", " ", sql).strip()

        for view, expected in _V61_VIEW_SQL.items():
            db_sql = _view_sql(db_at_v60_with_v59_views, view)
            assert db_sql is not None, f"view {view} missing after migrate_61"
            assert _normalize(db_sql) == _normalize(expected), (
                f"view {view} does not match frozen v61 snapshot"
            )

    def test_idempotent_when_already_at_v61(self, db_path, config_path):
        """Fresh DB ships v61+; re-running migrate_61 is a no-op short-circuit."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 61")
        conn.commit()
        conn.close()

        version_before = tusk_migrate.get_version(str(db_path))
        tusk_migrate.migrate_61(str(db_path), config_path, SCRIPT_DIR)

        assert tusk_migrate.get_version(str(db_path)) == version_before
