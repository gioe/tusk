"""Integration test for migrate_52: add code_reviews.model and backfill from task_sessions.

Covers:
- column addition (nullable TEXT)
- backfill: review's task_id + created_at within session window → lift ts.model
- reviews whose task has no matching session window remain NULL
- reviews that already carry a model value are not overwritten
- open task_sessions (ended_at IS NULL) extend to the present
- ambiguous overlaps resolve to the most recently started session
- schema version advances 51 → 52
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
def db_at_v51(db_path, config_path):
    """Reset the fresh-init DB back to version 51 so migrate_52 will run.

    Also drops the model column from code_reviews if it exists (fresh DBs ship
    with it as of v52), so the migration's ALTER TABLE path is exercised."""
    conn = sqlite3.connect(str(db_path))
    cols = [row[1] for row in conn.execute("PRAGMA table_info(code_reviews)").fetchall()]
    if "model" in cols:
        conn.executescript("""
            CREATE TABLE code_reviews_tmp (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                reviewer TEXT,
                status TEXT DEFAULT 'pending'
                    CHECK (status IN ('pending', 'in_progress', 'approved', 'changes_requested', 'superseded')),
                review_pass INTEGER DEFAULT 1,
                diff_summary TEXT,
                cost_dollars REAL,
                tokens_in INTEGER,
                tokens_out INTEGER,
                agent_name TEXT,
                note TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );
            INSERT INTO code_reviews_tmp
              SELECT id, task_id, reviewer, status, review_pass, diff_summary,
                     cost_dollars, tokens_in, tokens_out, agent_name, note,
                     created_at, updated_at
                FROM code_reviews;
            DROP TABLE code_reviews;
            ALTER TABLE code_reviews_tmp RENAME TO code_reviews;
            CREATE INDEX idx_code_reviews_task_id ON code_reviews(task_id);
        """)
    conn.execute("PRAGMA user_version = 51")
    conn.commit()
    conn.close()
    return str(db_path)


def _seed_task(db, task_id):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (task_id, f"t{task_id}"))
    conn.commit()
    conn.close()


def _seed_session(db, task_id, started_at, ended_at, model):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO task_sessions (task_id, started_at, ended_at, model) VALUES (?, ?, ?, ?)",
        (task_id, started_at, ended_at, model),
    )
    conn.commit()
    conn.close()


def _seed_review(db, task_id, created_at, model=None):
    conn = sqlite3.connect(db)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(code_reviews)").fetchall()]
    if "model" in cols and model is not None:
        conn.execute(
            "INSERT INTO code_reviews (task_id, created_at, model) VALUES (?, ?, ?)",
            (task_id, created_at, model),
        )
    else:
        conn.execute(
            "INSERT INTO code_reviews (task_id, created_at) VALUES (?, ?)",
            (task_id, created_at),
        )
    conn.commit()
    conn.close()


def _fetch_reviews(db):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT task_id, created_at, model FROM code_reviews ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


class TestMigrate52:

    def test_adds_model_column(self, db_at_v51, config_path):
        tusk_migrate.migrate_52(db_at_v51, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v51)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(code_reviews)").fetchall()]
        conn.close()
        assert "model" in cols

    def test_backfills_model_from_matching_session(self, db_at_v51, config_path):
        _seed_task(db_at_v51, 101)
        _seed_session(db_at_v51, 101, "2026-04-18 10:00:00", "2026-04-18 11:00:00", "claude-opus-4-7")
        _seed_review(db_at_v51, 101, "2026-04-18 10:30:00")

        tusk_migrate.migrate_52(db_at_v51, config_path, SCRIPT_DIR)

        assert _fetch_reviews(db_at_v51) == [
            (101, "2026-04-18 10:30:00", "claude-opus-4-7"),
        ]

    def test_leaves_review_without_matching_session_null(self, db_at_v51, config_path):
        _seed_task(db_at_v51, 102)
        _seed_session(db_at_v51, 102, "2026-04-18 10:00:00", "2026-04-18 11:00:00", "claude-opus-4-7")
        # review created before any session was opened for this task
        _seed_review(db_at_v51, 102, "2026-04-18 09:00:00")

        tusk_migrate.migrate_52(db_at_v51, config_path, SCRIPT_DIR)

        assert _fetch_reviews(db_at_v51) == [
            (102, "2026-04-18 09:00:00", None),
        ]

    def test_does_not_match_other_tasks_session(self, db_at_v51, config_path):
        # Two tasks; the session window overlaps but belongs to a different task.
        _seed_task(db_at_v51, 103)
        _seed_task(db_at_v51, 104)
        _seed_session(db_at_v51, 103, "2026-04-18 10:00:00", "2026-04-18 11:00:00", "claude-opus-4-7")
        _seed_review(db_at_v51, 104, "2026-04-18 10:30:00")

        tusk_migrate.migrate_52(db_at_v51, config_path, SCRIPT_DIR)

        assert _fetch_reviews(db_at_v51) == [
            (104, "2026-04-18 10:30:00", None),
        ]

    def test_open_session_extends_to_now(self, db_at_v51, config_path):
        _seed_task(db_at_v51, 105)
        _seed_session(db_at_v51, 105, "2026-04-18 10:00:00", None, "claude-sonnet-4-6")
        _seed_review(db_at_v51, 105, "2026-04-18 10:30:00")

        tusk_migrate.migrate_52(db_at_v51, config_path, SCRIPT_DIR)

        assert _fetch_reviews(db_at_v51) == [
            (105, "2026-04-18 10:30:00", "claude-sonnet-4-6"),
        ]

    def test_ambiguous_overlap_picks_most_recently_started(self, db_at_v51, config_path):
        _seed_task(db_at_v51, 201)
        # Two overlapping sessions for the same task; review at 10:45 falls inside both.
        _seed_session(db_at_v51, 201, "2026-04-18 10:00:00", "2026-04-18 11:00:00", "claude-opus-4-6")
        _seed_session(db_at_v51, 201, "2026-04-18 10:30:00", "2026-04-18 11:30:00", "claude-opus-4-7")
        _seed_review(db_at_v51, 201, "2026-04-18 10:45:00")

        tusk_migrate.migrate_52(db_at_v51, config_path, SCRIPT_DIR)

        # Second session started later, so its model wins.
        assert _fetch_reviews(db_at_v51) == [
            (201, "2026-04-18 10:45:00", "claude-opus-4-7"),
        ]

    def test_does_not_overwrite_existing_model(self, db_at_v51, config_path):
        # Run the migration once to add the column, then seed a pre-populated row.
        tusk_migrate.migrate_52(db_at_v51, config_path, SCRIPT_DIR)
        # Reset version so we can run the migration again.
        conn = sqlite3.connect(db_at_v51)
        conn.execute("PRAGMA user_version = 51")
        conn.commit()
        conn.close()

        _seed_task(db_at_v51, 301)
        # Session has a different model; backfill would re-attribute if it
        # didn't respect existing non-NULL values.
        _seed_session(db_at_v51, 301, "2026-04-18 10:00:00", "2026-04-18 11:00:00", "claude-opus-4-6")
        _seed_review(db_at_v51, 301, "2026-04-18 10:30:00", model="claude-sonnet-4-6")

        tusk_migrate.migrate_52(db_at_v51, config_path, SCRIPT_DIR)

        assert _fetch_reviews(db_at_v51) == [
            (301, "2026-04-18 10:30:00", "claude-sonnet-4-6"),
        ]

    def test_advances_schema_version_to_52(self, db_at_v51, config_path):
        assert tusk_migrate.get_version(db_at_v51) == 51
        tusk_migrate.migrate_52(db_at_v51, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db_at_v51) == 52

    def test_idempotent_when_already_at_v52(self, db_path, config_path):
        """Fresh DB already has the column present; stamp v52 so the idempotent
        path stays version-independent of future migrations."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 52")
        conn.commit()
        conn.close()

        _seed_task(str(db_path), 401)
        _seed_session(str(db_path), 401, "2026-04-18 10:00:00", "2026-04-18 11:00:00", "claude-opus-4-7")
        # Insert with model NULL — the guard should short-circuit and leave it NULL.
        _seed_review(str(db_path), 401, "2026-04-18 10:30:00")

        tusk_migrate.migrate_52(str(db_path), config_path, SCRIPT_DIR)

        assert tusk_migrate.get_version(str(db_path)) == 52
        assert _fetch_reviews(str(db_path)) == [
            (401, "2026-04-18 10:30:00", None),
        ]
