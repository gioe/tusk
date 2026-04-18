"""Integration test for migrate_54: broaden task_metrics.reopen_count to
count any backward jump into To Do (to_status = 'To Do').

Covers:
- view recreation with the broadened predicate
- In Progress -> To Do rework counts (the TASK-94 regression)
- Done -> To Do post-Done reopen still counts (preserves migrate_53 behavior)
- multiple backward jumps into To Do accumulate
- straight-through task (To Do -> In Progress -> Done) has reopen_count = 0
- historical backfill from migrate_53 produces no to_status='To Do' rows,
  so pre-migration Done tasks still read as 0
- schema version advances 53 -> 54
- idempotent short-circuit on re-run
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
def db_at_v53(db_path):
    """Reset a fresh-init DB back to version 53 with the narrow task_metrics
    view so migrate_54 exercises its view-recreation path from scratch."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        DROP VIEW IF EXISTS task_metrics;
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
              WHERE tst.task_id = t.id AND tst.from_status IN ('Done')) as reopen_count
        FROM tasks t
        LEFT JOIN task_sessions s ON t.id = s.task_id
        GROUP BY t.id;
    """)
    conn.execute("PRAGMA user_version = 53")
    conn.commit()
    conn.close()
    return str(db_path)


def _seed_task(db, task_id, status="To Do", started_at=None, closed_at=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO tasks (id, summary, status, started_at, closed_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, f"t{task_id}", status, started_at, closed_at),
    )
    conn.commit()
    conn.close()


def _drop_forward_only_trigger(db):
    # validate_status_transition blocks backward transitions. The real
    # 'tusk task-reopen --force' drops and re-creates it; mirror that here so
    # we can execute In Progress -> To Do and Done -> To Do updates directly.
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER IF EXISTS validate_status_transition")
    conn.commit()
    conn.close()


def _reopen_count(db, task_id):
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT reopen_count FROM task_metrics WHERE id = ?", (task_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


class TestMigrate54:

    def test_advances_schema_version_to_54(self, db_at_v53, config_path):
        assert tusk_migrate.get_version(db_at_v53) == 53
        tusk_migrate.migrate_54(db_at_v53, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db_at_v53) == 54

    def test_view_uses_broadened_predicate(self, db_at_v53, config_path):
        """After migrate_54 the task_metrics view's reopen_count subquery
        must reference to_status = 'To Do', not from_status IN ('Done')."""
        tusk_migrate.migrate_54(db_at_v53, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v53)
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name='task_metrics'"
        ).fetchone()[0]
        conn.close()

        assert "to_status = 'To Do'" in sql
        assert "from_status IN ('Done')" not in sql

    def test_in_progress_to_todo_rework_counts_as_reopen(
        self, db_at_v53, config_path
    ):
        """Regression for TASK-94: a task that bounces In Progress -> To Do
        without ever reaching Done must register reopen_count = 1.

        The narrow predicate from migrate_53 (from_status = 'Done') misses
        this cycle because from_status is 'In Progress' on the rework hop."""
        tusk_migrate.migrate_54(db_at_v53, config_path, SCRIPT_DIR)
        _drop_forward_only_trigger(db_at_v53)

        # Full path: To Do -> In Progress -> To Do -> In Progress -> Done.
        # The only hop with to_status='To Do' is the middle rework.
        _seed_task(db_at_v53, 7001, status="To Do")
        conn = sqlite3.connect(db_at_v53)
        conn.execute("UPDATE tasks SET status = 'In Progress' WHERE id = 7001")
        conn.execute("UPDATE tasks SET status = 'To Do' WHERE id = 7001")
        conn.execute("UPDATE tasks SET status = 'In Progress' WHERE id = 7001")
        conn.execute("UPDATE tasks SET status = 'Done' WHERE id = 7001")
        conn.commit()
        conn.close()

        assert _reopen_count(db_at_v53, 7001) == 1

    def test_done_to_todo_post_done_reopen_still_counts(
        self, db_at_v53, config_path
    ):
        """The broadened predicate must preserve migrate_53 behavior: a
        post-Done reopen via 'tusk task-reopen --force' (Done -> To Do) still
        counts, because to_status = 'To Do' captures it."""
        tusk_migrate.migrate_54(db_at_v53, config_path, SCRIPT_DIR)
        _drop_forward_only_trigger(db_at_v53)

        _seed_task(db_at_v53, 7002, status="To Do")
        conn = sqlite3.connect(db_at_v53)
        conn.execute("UPDATE tasks SET status = 'In Progress' WHERE id = 7002")
        conn.execute("UPDATE tasks SET status = 'Done' WHERE id = 7002")
        conn.execute("UPDATE tasks SET status = 'To Do' WHERE id = 7002")
        conn.execute("UPDATE tasks SET status = 'In Progress' WHERE id = 7002")
        conn.execute("UPDATE tasks SET status = 'Done' WHERE id = 7002")
        conn.commit()
        conn.close()

        assert _reopen_count(db_at_v53, 7002) == 1

    def test_multiple_backward_jumps_accumulate(self, db_at_v53, config_path):
        tusk_migrate.migrate_54(db_at_v53, config_path, SCRIPT_DIR)
        _drop_forward_only_trigger(db_at_v53)

        # One In Progress -> To Do and one Done -> To Do on the same task.
        _seed_task(db_at_v53, 7003, status="To Do")
        conn = sqlite3.connect(db_at_v53)
        conn.execute("UPDATE tasks SET status = 'In Progress' WHERE id = 7003")
        conn.execute("UPDATE tasks SET status = 'To Do' WHERE id = 7003")  # rework
        conn.execute("UPDATE tasks SET status = 'In Progress' WHERE id = 7003")
        conn.execute("UPDATE tasks SET status = 'Done' WHERE id = 7003")
        conn.execute("UPDATE tasks SET status = 'To Do' WHERE id = 7003")  # post-Done reopen
        conn.commit()
        conn.close()

        assert _reopen_count(db_at_v53, 7003) == 2

    def test_straight_through_task_has_zero_reopens(self, db_at_v53, config_path):
        tusk_migrate.migrate_54(db_at_v53, config_path, SCRIPT_DIR)

        _seed_task(db_at_v53, 7004, status="To Do")
        conn = sqlite3.connect(db_at_v53)
        conn.execute("UPDATE tasks SET status = 'In Progress' WHERE id = 7004")
        conn.execute("UPDATE tasks SET status = 'Done' WHERE id = 7004")
        conn.commit()
        conn.close()

        assert _reopen_count(db_at_v53, 7004) == 0

    def test_historical_backfill_rows_do_not_inflate_reopen_count(
        self, db_at_v53, config_path
    ):
        """Migration 53's backfill seeds synthetic 'To Do -> In Progress' and
        'In Progress -> Done' rows — neither has to_status='To Do'. A Done
        task that existed before migrate_53 must still read reopen_count = 0
        under the broadened predicate, preserving the forward-looking-only
        property documented in DOMAIN.md."""
        # Seed synthetic backfill rows directly (simulating what migrate_53's
        # backfill produces for a pre-existing Done task).
        _seed_task(
            db_at_v53, 7005, status="Done",
            started_at="2026-04-10 10:00:00", closed_at="2026-04-10 12:00:00",
        )
        conn = sqlite3.connect(db_at_v53)
        conn.executescript("""
            INSERT INTO task_status_transitions (task_id, from_status, to_status, changed_at)
            VALUES (7005, 'To Do', 'In Progress', '2026-04-10 10:00:00');
            INSERT INTO task_status_transitions (task_id, from_status, to_status, changed_at)
            VALUES (7005, 'In Progress', 'Done', '2026-04-10 12:00:00');
        """)
        conn.commit()
        conn.close()

        tusk_migrate.migrate_54(db_at_v53, config_path, SCRIPT_DIR)
        assert _reopen_count(db_at_v53, 7005) == 0

    def test_idempotent_when_already_at_v54(self, db_path, config_path):
        """Fresh DB already ships at v54 with the broadened view. Stamp v54
        explicitly so the idempotent path stays version-independent of
        future migrations."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 54")
        conn.commit()
        conn.close()

        _seed_task(str(db_path), 7006, status="To Do")

        tusk_migrate.migrate_54(str(db_path), config_path, SCRIPT_DIR)

        assert tusk_migrate.get_version(str(db_path)) == 54
        assert _reopen_count(str(db_path), 7006) == 0
