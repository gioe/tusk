"""Integration test for migrate_69: add code_reviews.diff_range column."""

import importlib.util
import os
import sqlite3


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


def _columns(db, table):
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return [r[1] for r in rows]


def test_migrate_69_adds_diff_range_column(db_path, config_path):
    """Migration 69 must add ``diff_range`` to ``code_reviews`` and stamp
    ``user_version = 69``. Pre-migration rows stay NULL."""
    db = str(db_path)
    conn = sqlite3.connect(db)
    # Drop the column to simulate a pre-migration DB even when the fresh-init
    # schema in cmd_init already includes it.
    conn.execute("CREATE TABLE _code_reviews_pre69 AS SELECT * FROM code_reviews")
    conn.execute("DROP TABLE code_reviews")
    conn.execute(
        """
        CREATE TABLE code_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            reviewer TEXT,
            status TEXT DEFAULT 'pending'
                CHECK (status IN ('pending', 'in_progress', 'approved',
                                  'changes_requested', 'superseded')),
            review_pass INTEGER DEFAULT 1,
            diff_summary TEXT,
            cost_dollars REAL,
            tokens_in INTEGER,
            tokens_out INTEGER,
            agent_name TEXT,
            model TEXT,
            note TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("INSERT INTO tasks (summary) VALUES ('seed task')")
    conn.execute(
        "INSERT INTO code_reviews (task_id, status, diff_summary)"
        " VALUES (1, 'pending', 'historical summary')"
    )
    conn.execute("PRAGMA user_version = 68")
    conn.commit()
    conn.close()

    tusk_migrate.migrate_69(db, config_path, SCRIPT_DIR)

    assert tusk_migrate.get_version(db) == 69
    cols = _columns(db, "code_reviews")
    assert "diff_range" in cols

    # Historical row's diff_range stays NULL — back-compat for pre-v69 reviews.
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT diff_range FROM code_reviews WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] is None


def test_migrate_69_idempotent_when_already_at_v69(db_path, config_path):
    """Running migration 69 against a DB already stamped to v69 is a no-op."""
    db = str(db_path)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 69")
    conn.commit()
    conn.close()

    tusk_migrate.migrate_69(db, config_path, SCRIPT_DIR)

    assert tusk_migrate.get_version(db) >= 69
