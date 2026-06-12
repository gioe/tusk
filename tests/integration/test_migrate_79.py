"""Integration test for migration 79: task_sessions.active_seconds."""

from __future__ import annotations

import importlib.util
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIGRATE_PATH = os.path.join(REPO_ROOT, "bin", "tusk-migrate.py")

_spec = importlib.util.spec_from_file_location("tusk_migrate", MIGRATE_PATH)
tusk_migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tusk_migrate)


def _create_v78_shape(db_path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS task_sessions;

            CREATE TABLE task_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                duration_seconds INTEGER,
                cost_dollars REAL,
                tokens_in INTEGER,
                tokens_out INTEGER,
                lines_added INTEGER,
                lines_removed INTEGER,
                model TEXT
            );
            PRAGMA user_version = 78;
            """
        )
        conn.commit()
    finally:
        conn.close()


def _columns(db_path) -> set:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(task_sessions)").fetchall()}
    finally:
        conn.close()


def test_migrate_79_adds_active_seconds(db_path, config_path):
    _create_v78_shape(db_path)

    tusk_migrate.migrate_79(str(db_path), str(config_path), os.path.join(REPO_ROOT, "bin"))

    conn = sqlite3.connect(str(db_path))
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(task_sessions)").fetchall()}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()

    assert "active_seconds" in columns
    assert version == 79


def test_fresh_init_carries_column(db_path):
    # The db_path fixture initializes via tusk init — the column must be in
    # cmd_init's CREATE TABLE so fresh installs never need migration 79.
    assert "active_seconds" in _columns(db_path)


def test_idempotent_when_already_at_v79(db_path, config_path):
    # Fresh DBs initialize at the latest schema; stamp 79 explicitly so this
    # test keeps passing when later migrations land (see CLAUDE.md checklist).
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA user_version = 79")
        conn.commit()
    finally:
        conn.close()

    before = _columns(db_path)
    assert "active_seconds" in before  # fresh init already carries the column

    tusk_migrate.migrate_79(str(db_path), str(config_path), os.path.join(REPO_ROOT, "bin"))

    after = _columns(db_path)
    assert after == before

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 79
    finally:
        conn.close()
