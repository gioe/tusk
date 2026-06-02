"""Integration test for migration 76: task_progress.note."""

from __future__ import annotations

import importlib.util
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIGRATE_PATH = os.path.join(REPO_ROOT, "bin", "tusk-migrate.py")

_spec = importlib.util.spec_from_file_location("tusk_migrate", MIGRATE_PATH)
tusk_migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tusk_migrate)


def _create_v75_shape(db_path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS task_progress;

            CREATE TABLE task_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                commit_hash TEXT,
                commit_message TEXT,
                files_changed TEXT,
                next_steps TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            PRAGMA user_version = 75;
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_migrate_76_adds_task_progress_note(db_path, config_path):
    _create_v75_shape(db_path)

    tusk_migrate.migrate_76(str(db_path), str(config_path), os.path.join(REPO_ROOT, "bin"))

    conn = sqlite3.connect(str(db_path))
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(task_progress)").fetchall()}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()

    assert "note" in columns
    assert version == 76
