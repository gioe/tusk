"""Integration test for migrate_51: add skill_runs.task_id and backfill task-scoped rows.

Covers:
- column addition (nullable FK → tasks.id ON DELETE SET NULL)
- backfill: task-scoped skills join to task_sessions by time-window containment
- non-task-scoped skill_name rows are left NULL
- rows already carrying task_id are not overwritten
- open task_sessions (ended_at IS NULL) extend to the present
- ambiguous overlaps resolve to the most recently started session
- schema version advances 50 → 51
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
def db_at_v50(db_path, config_path):
    """Reset the fresh-init DB back to version 50 so migrate_51 will run.

    Also drops the task_id column from skill_runs if it exists (fresh DBs ship
    with it as of v51), so the migration's ALTER TABLE path is exercised."""
    conn = sqlite3.connect(str(db_path))
    cols = [row[1] for row in conn.execute("PRAGMA table_info(skill_runs)").fetchall()]
    if "task_id" in cols:
        conn.executescript("""
            CREATE TABLE skill_runs_tmp (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_name TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                ended_at TEXT,
                cost_dollars REAL,
                tokens_in INTEGER,
                tokens_out INTEGER,
                model TEXT,
                metadata TEXT,
                request_count INTEGER
            );
            INSERT INTO skill_runs_tmp
              SELECT id, skill_name, started_at, ended_at, cost_dollars,
                     tokens_in, tokens_out, model, metadata, request_count
                FROM skill_runs;
            DROP TABLE skill_runs;
            ALTER TABLE skill_runs_tmp RENAME TO skill_runs;
            CREATE INDEX idx_skill_runs_skill_name ON skill_runs(skill_name);
        """)
    conn.execute("PRAGMA user_version = 50")
    conn.commit()
    conn.close()
    return str(db_path)


def _seed_task(db, task_id):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (task_id, f"t{task_id}"))
    conn.commit()
    conn.close()


def _seed_session(db, task_id, started_at, ended_at):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO task_sessions (task_id, started_at, ended_at) VALUES (?, ?, ?)",
        (task_id, started_at, ended_at),
    )
    conn.commit()
    conn.close()


def _seed_skill_run(db, skill_name, started_at, task_id=None):
    conn = sqlite3.connect(db)
    if task_id is None:
        conn.execute(
            "INSERT INTO skill_runs (skill_name, started_at) VALUES (?, ?)",
            (skill_name, started_at),
        )
    else:
        # Used after the column exists — the fixture runs the migration first
        # for this scenario.
        conn.execute(
            "INSERT INTO skill_runs (skill_name, started_at, task_id) VALUES (?, ?, ?)",
            (skill_name, started_at, task_id),
        )
    conn.commit()
    conn.close()


def _fetch_skill_runs(db):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT skill_name, started_at, task_id FROM skill_runs ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


class TestMigrate51:

    def test_adds_task_id_column(self, db_at_v50, config_path):
        tusk_migrate.migrate_51(db_at_v50, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v50)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(skill_runs)").fetchall()]
        conn.close()
        assert "task_id" in cols

    def test_backfills_task_scoped_skill_runs_via_time_window(self, db_at_v50, config_path):
        _seed_task(db_at_v50, 101)
        _seed_session(db_at_v50, 101, "2026-04-18 10:00:00", "2026-04-18 11:00:00")
        _seed_skill_run(db_at_v50, "review-commits", "2026-04-18 10:30:00")
        _seed_skill_run(db_at_v50, "tusk", "2026-04-18 10:45:00")

        tusk_migrate.migrate_51(db_at_v50, config_path, SCRIPT_DIR)

        assert _fetch_skill_runs(db_at_v50) == [
            ("review-commits", "2026-04-18 10:30:00", 101),
            ("tusk", "2026-04-18 10:45:00", 101),
        ]

    def test_leaves_non_task_scoped_skills_null(self, db_at_v50, config_path):
        _seed_task(db_at_v50, 102)
        _seed_session(db_at_v50, 102, "2026-04-18 10:00:00", "2026-04-18 11:00:00")
        _seed_skill_run(db_at_v50, "groom-backlog", "2026-04-18 10:30:00")
        _seed_skill_run(db_at_v50, "investigate", "2026-04-18 10:31:00")
        _seed_skill_run(db_at_v50, "investigate-directory", "2026-04-18 10:32:00")

        tusk_migrate.migrate_51(db_at_v50, config_path, SCRIPT_DIR)

        assert _fetch_skill_runs(db_at_v50) == [
            ("groom-backlog", "2026-04-18 10:30:00", None),
            ("investigate", "2026-04-18 10:31:00", None),
            ("investigate-directory", "2026-04-18 10:32:00", None),
        ]

    def test_leaves_skill_run_outside_any_session_null(self, db_at_v50, config_path):
        _seed_task(db_at_v50, 103)
        _seed_session(db_at_v50, 103, "2026-04-18 10:00:00", "2026-04-18 11:00:00")
        # skill_run started before any session was opened
        _seed_skill_run(db_at_v50, "tusk", "2026-04-18 09:00:00")

        tusk_migrate.migrate_51(db_at_v50, config_path, SCRIPT_DIR)

        assert _fetch_skill_runs(db_at_v50) == [
            ("tusk", "2026-04-18 09:00:00", None),
        ]

    def test_open_session_extends_to_now(self, db_at_v50, config_path):
        _seed_task(db_at_v50, 104)
        _seed_session(db_at_v50, 104, "2026-04-18 10:00:00", None)
        _seed_skill_run(db_at_v50, "review-commits", "2026-04-18 10:30:00")

        tusk_migrate.migrate_51(db_at_v50, config_path, SCRIPT_DIR)

        assert _fetch_skill_runs(db_at_v50) == [
            ("review-commits", "2026-04-18 10:30:00", 104),
        ]

    def test_ambiguous_overlap_picks_most_recently_started(self, db_at_v50, config_path):
        _seed_task(db_at_v50, 201)
        _seed_task(db_at_v50, 202)
        # Two overlapping sessions; skill_run at 10:45 falls inside both.
        _seed_session(db_at_v50, 201, "2026-04-18 10:00:00", "2026-04-18 11:00:00")
        _seed_session(db_at_v50, 202, "2026-04-18 10:30:00", "2026-04-18 11:30:00")
        _seed_skill_run(db_at_v50, "chain", "2026-04-18 10:45:00")

        tusk_migrate.migrate_51(db_at_v50, config_path, SCRIPT_DIR)

        # 202 started later, so it wins.
        assert _fetch_skill_runs(db_at_v50) == [
            ("chain", "2026-04-18 10:45:00", 202),
        ]

    def test_does_not_overwrite_existing_task_id(self, db_at_v50, config_path):
        # Run the migration once to add the column, then seed a pre-populated row.
        tusk_migrate.migrate_51(db_at_v50, config_path, SCRIPT_DIR)
        # Reset version so we can run the migration again.
        conn = sqlite3.connect(db_at_v50)
        conn.execute("PRAGMA user_version = 50")
        conn.commit()
        conn.close()

        _seed_task(db_at_v50, 301)
        _seed_task(db_at_v50, 302)
        _seed_session(db_at_v50, 302, "2026-04-18 10:00:00", "2026-04-18 11:00:00")
        # Skill run says it was for task 301; session-window join would
        # re-attribute it to 302 if the migration didn't respect existing values.
        _seed_skill_run(db_at_v50, "review-commits", "2026-04-18 10:30:00", task_id=301)

        tusk_migrate.migrate_51(db_at_v50, config_path, SCRIPT_DIR)

        assert _fetch_skill_runs(db_at_v50) == [
            ("review-commits", "2026-04-18 10:30:00", 301),
        ]

    def test_advances_schema_version_to_51(self, db_at_v50, config_path):
        assert tusk_migrate.get_version(db_at_v50) == 50
        tusk_migrate.migrate_51(db_at_v50, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db_at_v50) == 51

    def test_idempotent_when_already_at_v51(self, db_path, config_path):
        """Fresh DB is already at v51+ with the column present; re-running is a no-op."""
        assert tusk_migrate.get_version(str(db_path)) >= 51
        _seed_task(str(db_path), 401)
        _seed_session(str(db_path), 401, "2026-04-18 10:00:00", "2026-04-18 11:00:00")
        # Insert with task_id NULL — the guard should short-circuit and leave it NULL.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO skill_runs (skill_name, started_at) VALUES (?, ?)",
            ("review-commits", "2026-04-18 10:30:00"),
        )
        conn.commit()
        conn.close()

        version_before = tusk_migrate.get_version(str(db_path))
        tusk_migrate.migrate_51(str(db_path), config_path, SCRIPT_DIR)

        assert tusk_migrate.get_version(str(db_path)) == version_before
        assert _fetch_skill_runs(str(db_path)) == [
            ("review-commits", "2026-04-18 10:30:00", None),
        ]
