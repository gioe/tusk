"""Integration test for migration 78: external_blockers.resolution_note."""

from __future__ import annotations

import importlib.util
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIGRATE_PATH = os.path.join(REPO_ROOT, "bin", "tusk-migrate.py")

_spec = importlib.util.spec_from_file_location("tusk_migrate", MIGRATE_PATH)
tusk_migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tusk_migrate)


def _create_v77_shape(db_path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS external_blockers;

            CREATE TABLE external_blockers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                blocker_type TEXT,
                is_resolved INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                resolved_at TEXT,
                CHECK (is_resolved IN (0, 1))
            );
            PRAGMA user_version = 77;
            """
        )
        conn.commit()
    finally:
        conn.close()


def _columns(db_path) -> set:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(external_blockers)").fetchall()}
    finally:
        conn.close()


def test_migrate_78_adds_resolution_note(db_path, config_path):
    _create_v77_shape(db_path)

    tusk_migrate.migrate_78(str(db_path), str(config_path), os.path.join(REPO_ROOT, "bin"))

    conn = sqlite3.connect(str(db_path))
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(external_blockers)").fetchall()}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()

    assert "resolution_note" in columns
    assert version == 78


def test_idempotent_when_already_at_v78(db_path, config_path):
    # Fresh DBs initialize at the latest schema; stamp 78 explicitly so this
    # test keeps passing when later migrations land (see CLAUDE.md checklist).
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA user_version = 78")
        conn.commit()
    finally:
        conn.close()

    before = _columns(db_path)
    assert "resolution_note" in before  # fresh init already carries the column

    tusk_migrate.migrate_78(str(db_path), str(config_path), os.path.join(REPO_ROOT, "bin"))

    assert _columns(db_path) == before
