"""Integration tests for migration 88: immutable task-scope checkpoints."""

from __future__ import annotations

import importlib.util
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIGRATE_PATH = os.path.join(REPO_ROOT, "bin", "tusk-migrate.py")
_spec = importlib.util.spec_from_file_location("tusk_migrate", MIGRATE_PATH)
tusk_migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tusk_migrate)
BIN_DIR = os.path.join(REPO_ROOT, "bin")


def _legacy_db(path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE tasks (id INTEGER PRIMARY KEY);
            CREATE TABLE task_scope (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                pattern TEXT NOT NULL,
                source TEXT NOT NULL,
                reason TEXT,
                locked_at TEXT,
                locked_by TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );
            INSERT INTO tasks (id) VALUES (1), (2), (3);
            INSERT INTO task_scope
                (task_id, pattern, source, locked_at, locked_by)
            VALUES
                (1, 'later', 'operator_declared', '2026-08-06 15:00:00', 'later-actor'),
                (1, 'earlier', 'operator_declared', '2026-08-06 14:00:00', NULL),
                (2, 'loose', 'operator_declared', NULL, NULL);
            PRAGMA user_version = 87;
            """
        )
    finally:
        conn.close()


def test_migrate_88_backfills_earliest_historical_checkpoint(tmp_path, config_path):
    db_path = tmp_path / "tasks.db"
    _legacy_db(db_path)

    tusk_migrate.migrate_88(str(db_path), str(config_path), BIN_DIR)

    conn = sqlite3.connect(str(db_path))
    try:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(task_scope_checkpoints)")
        }
        assert columns == {"task_id", "locked_at", "locked_by"}
        rows = conn.execute(
            "SELECT task_id, locked_at, locked_by "
            "FROM task_scope_checkpoints ORDER BY task_id"
        ).fetchall()
        assert rows == [(1, "2026-08-06 14:00:00", "unknown")]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 88
    finally:
        conn.close()


def test_migrate_88_is_idempotent_after_partial_table_creation(tmp_path, config_path):
    db_path = tmp_path / "tasks.db"
    _legacy_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE task_scope_checkpoints ("
            "task_id INTEGER PRIMARY KEY, locked_at TEXT NOT NULL, "
            "locked_by TEXT NOT NULL, FOREIGN KEY (task_id) "
            "REFERENCES tasks(id) ON DELETE CASCADE)"
        )
        conn.commit()
    finally:
        conn.close()

    tusk_migrate.migrate_88(str(db_path), str(config_path), BIN_DIR)
    tusk_migrate.migrate_88(str(db_path), str(config_path), BIN_DIR)

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_scope_checkpoints"
        ).fetchone()[0] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 88
    finally:
        conn.close()


def test_fresh_init_is_at_or_past_v88_and_has_checkpoint_table(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 88
        assert conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'task_scope_checkpoints'"
        ).fetchone() is not None
    finally:
        conn.close()
