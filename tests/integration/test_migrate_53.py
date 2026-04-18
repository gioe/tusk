"""Integration test for migrate_53: add task_status_transitions + reopen_count.

Covers:
- table + index creation (idempotent via has_table guard)
- AFTER UPDATE OF status trigger fires on a real status change
- trigger does NOT fire on a no-op UPDATE where OLD.status == NEW.status
- backfill seeds synthetic 'To Do -> In Progress' / 'In Progress -> Done' rows
  for existing Done/In Progress tasks; no synthetic row has from_status='Done'
- backfill is idempotent (NOT EXISTS guards)
- task_metrics view exposes reopen_count and distinguishes a straight-through
  task from a reopened one
- schema version advances 52 -> 53
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
def db_at_v52(db_path):
    """Reset a fresh-init DB back to version 52 and drop the v53 artefacts so
    migrate_53 exercises its table-create, trigger-create, backfill, and view
    re-creation paths from scratch."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        DROP TRIGGER IF EXISTS log_task_status_transition;
        DROP TABLE IF EXISTS task_status_transitions;

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
            SUM(s.request_count) as total_request_count
        FROM tasks t
        LEFT JOIN task_sessions s ON t.id = s.task_id
        GROUP BY t.id;
    """)
    conn.execute("PRAGMA user_version = 52")
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


def _fetch_transitions(db, task_id=None):
    conn = sqlite3.connect(db)
    if task_id is None:
        rows = conn.execute(
            "SELECT task_id, from_status, to_status, changed_at FROM task_status_transitions ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT task_id, from_status, to_status, changed_at FROM task_status_transitions WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    conn.close()
    return rows


class TestMigrate53:

    def test_creates_table_and_index(self, db_at_v52, config_path):
        tusk_migrate.migrate_53(db_at_v52, config_path, SCRIPT_DIR)
        conn = sqlite3.connect(db_at_v52)
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='task_status_transitions'"
            ).fetchall()
        ]
        indexes = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_task_status_transitions_task_id'"
            ).fetchall()
        ]
        conn.close()
        assert tables == ["task_status_transitions"]
        assert indexes == ["idx_task_status_transitions_task_id"]

    def test_creates_trigger(self, db_at_v52, config_path):
        tusk_migrate.migrate_53(db_at_v52, config_path, SCRIPT_DIR)
        conn = sqlite3.connect(db_at_v52)
        triggers = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name='log_task_status_transition'"
            ).fetchall()
        ]
        conn.close()
        assert triggers == ["log_task_status_transition"]

    def test_trigger_logs_status_change(self, db_at_v52, config_path):
        tusk_migrate.migrate_53(db_at_v52, config_path, SCRIPT_DIR)
        _seed_task(db_at_v52, 1001, status="To Do")
        conn = sqlite3.connect(db_at_v52)
        conn.execute("UPDATE tasks SET status = 'In Progress' WHERE id = 1001")
        conn.commit()
        conn.close()

        rows = _fetch_transitions(db_at_v52, task_id=1001)
        assert [(r[0], r[1], r[2]) for r in rows] == [(1001, "To Do", "In Progress")]
        # changed_at is stamped
        assert rows[0][3] is not None

    def test_trigger_does_not_fire_on_noop_update(self, db_at_v52, config_path):
        tusk_migrate.migrate_53(db_at_v52, config_path, SCRIPT_DIR)
        _seed_task(db_at_v52, 1002, status="In Progress")
        conn = sqlite3.connect(db_at_v52)
        # Same-status assignment: WHEN clause blocks the insert.
        conn.execute("UPDATE tasks SET status = 'In Progress' WHERE id = 1002")
        conn.commit()
        conn.close()
        assert _fetch_transitions(db_at_v52, task_id=1002) == []

    def test_backfill_seeds_done_task(self, db_at_v52, config_path):
        _seed_task(
            db_at_v52,
            2001,
            status="Done",
            started_at="2026-04-10 10:00:00",
            closed_at="2026-04-10 12:00:00",
        )
        tusk_migrate.migrate_53(db_at_v52, config_path, SCRIPT_DIR)

        rows = _fetch_transitions(db_at_v52, task_id=2001)
        assert [(r[1], r[2], r[3]) for r in rows] == [
            ("To Do", "In Progress", "2026-04-10 10:00:00"),
            ("In Progress", "Done", "2026-04-10 12:00:00"),
        ]

    def test_backfill_done_task_without_started_at(self, db_at_v52, config_path):
        # Done task without started_at should only get the 'In Progress -> Done' row.
        _seed_task(
            db_at_v52,
            2002,
            status="Done",
            started_at=None,
            closed_at="2026-04-10 12:00:00",
        )
        tusk_migrate.migrate_53(db_at_v52, config_path, SCRIPT_DIR)

        rows = _fetch_transitions(db_at_v52, task_id=2002)
        assert [(r[1], r[2]) for r in rows] == [("In Progress", "Done")]

    def test_backfill_done_task_without_closed_at_falls_back_to_updated_at(
        self, db_at_v52, config_path
    ):
        # closed_at NULL => COALESCE(closed_at, updated_at) picks updated_at.
        _seed_task(db_at_v52, 2003, status="Done", started_at="2026-04-10 10:00:00")
        # Force a known updated_at value
        conn = sqlite3.connect(db_at_v52)
        conn.execute("UPDATE tasks SET updated_at = '2026-04-11 09:00:00' WHERE id = 2003")
        conn.commit()
        conn.close()

        tusk_migrate.migrate_53(db_at_v52, config_path, SCRIPT_DIR)
        rows = _fetch_transitions(db_at_v52, task_id=2003)
        # Second row is 'In Progress -> Done' at updated_at
        assert rows[-1][1:4] == ("In Progress", "Done", "2026-04-11 09:00:00")

    def test_backfill_seeds_in_progress_task(self, db_at_v52, config_path):
        _seed_task(
            db_at_v52,
            2010,
            status="In Progress",
            started_at="2026-04-10 10:00:00",
        )
        tusk_migrate.migrate_53(db_at_v52, config_path, SCRIPT_DIR)

        rows = _fetch_transitions(db_at_v52, task_id=2010)
        assert [(r[1], r[2], r[3]) for r in rows] == [
            ("To Do", "In Progress", "2026-04-10 10:00:00"),
        ]

    def test_backfill_skips_todo_task(self, db_at_v52, config_path):
        _seed_task(db_at_v52, 2020, status="To Do")
        tusk_migrate.migrate_53(db_at_v52, config_path, SCRIPT_DIR)
        assert _fetch_transitions(db_at_v52, task_id=2020) == []

    def test_backfill_never_produces_reopen_rows(self, db_at_v52, config_path):
        # Mix of Done/In Progress/To Do tasks — no synthetic row should have
        # from_status='Done'.
        _seed_task(
            db_at_v52, 3001, status="Done",
            started_at="2026-04-10 10:00:00", closed_at="2026-04-10 12:00:00",
        )
        _seed_task(db_at_v52, 3002, status="In Progress", started_at="2026-04-10 13:00:00")
        _seed_task(db_at_v52, 3003, status="To Do")

        tusk_migrate.migrate_53(db_at_v52, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v52)
        done_origins = conn.execute(
            "SELECT COUNT(*) FROM task_status_transitions WHERE from_status IN ('Done')"
        ).fetchone()[0]
        conn.close()
        assert done_origins == 0

    def test_backfill_is_idempotent(self, db_at_v52, config_path):
        _seed_task(
            db_at_v52, 4001, status="Done",
            started_at="2026-04-10 10:00:00", closed_at="2026-04-10 12:00:00",
        )
        tusk_migrate.migrate_53(db_at_v52, config_path, SCRIPT_DIR)
        first = _fetch_transitions(db_at_v52, task_id=4001)

        # Reset version so the function body re-runs, including backfill.
        conn = sqlite3.connect(db_at_v52)
        conn.execute("PRAGMA user_version = 52")
        conn.commit()
        conn.close()

        tusk_migrate.migrate_53(db_at_v52, config_path, SCRIPT_DIR)
        second = _fetch_transitions(db_at_v52, task_id=4001)
        assert first == second  # NOT EXISTS guard prevents duplicate inserts

    def test_reopen_count_distinguishes_straight_through_from_reopened(
        self, db_at_v52, config_path
    ):
        tusk_migrate.migrate_53(db_at_v52, config_path, SCRIPT_DIR)

        # Straight-through task: To Do -> In Progress -> Done
        _seed_task(db_at_v52, 5001, status="To Do")
        conn = sqlite3.connect(db_at_v52)
        conn.execute("UPDATE tasks SET status = 'In Progress' WHERE id = 5001")
        conn.execute("UPDATE tasks SET status = 'Done' WHERE id = 5001")
        conn.commit()
        conn.close()

        # Reopened task: To Do -> In Progress -> Done -> [reopen via force] ->
        # To Do -> In Progress -> Done. The `Done -> To Do` hop is what `tusk
        # task-reopen --force` does: it drops validate_status_transition,
        # applies the UPDATE, and regenerates the trigger. Mirror that here.
        _seed_task(db_at_v52, 5002, status="To Do")
        conn = sqlite3.connect(db_at_v52)
        conn.execute("UPDATE tasks SET status = 'In Progress' WHERE id = 5002")
        conn.execute("UPDATE tasks SET status = 'Done' WHERE id = 5002")
        conn.execute("DROP TRIGGER IF EXISTS validate_status_transition")
        conn.execute("UPDATE tasks SET status = 'To Do' WHERE id = 5002")
        conn.execute("UPDATE tasks SET status = 'In Progress' WHERE id = 5002")
        conn.execute("UPDATE tasks SET status = 'Done' WHERE id = 5002")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(db_at_v52)
        straight = conn.execute(
            "SELECT reopen_count FROM task_metrics WHERE id = 5001"
        ).fetchone()[0]
        reopened = conn.execute(
            "SELECT reopen_count FROM task_metrics WHERE id = 5002"
        ).fetchone()[0]
        conn.close()

        assert straight == 0
        assert reopened == 1

    def test_advances_schema_version_to_53(self, db_at_v52, config_path):
        assert tusk_migrate.get_version(db_at_v52) == 52
        tusk_migrate.migrate_53(db_at_v52, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db_at_v52) == 53

    def test_idempotent_when_already_at_v53(self, db_path, config_path):
        """Fresh DB already ships at v53 with the table/trigger/view in place.
        Stamp v53 explicitly so the idempotent path stays version-independent
        of future migrations."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 53")
        conn.commit()
        conn.close()

        _seed_task(str(db_path), 6001, status="To Do")

        tusk_migrate.migrate_53(str(db_path), config_path, SCRIPT_DIR)

        # Short-circuit means no mutation happened; version still 53.
        assert tusk_migrate.get_version(str(db_path)) == 53
        # Table exists (from fresh init) but remains empty for this task.
        assert _fetch_transitions(str(db_path), task_id=6001) == []
