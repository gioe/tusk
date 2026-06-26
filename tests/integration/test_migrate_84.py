"""Integration test for migration 84: switch existing DBs to WAL journal mode.

Issue #1144 (follow-up to #1143). cmd_init has enabled WAL for fresh installs
since Feb 2026, but DBs created before that stayed in rollback-journal ('delete')
mode, where the SHARED->RESERVED lock upgrade returns SQLITE_BUSY immediately —
the contention #1143 retries around. migrate_84 switches existing delete-mode
DBs to WAL so readers no longer block writers.
"""

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


def _journal_mode(db_path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()


def _user_version(db_path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _downgrade_to_v83_delete(db_path) -> None:
    """Simulate a pre-WAL DB: roll back to rollback-journal mode and stamp v83.

    PRAGMA journal_mode cannot change inside a transaction, so use an autocommit
    connection.
    """
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA user_version = 83")
    finally:
        conn.close()


def test_migrate_84_switches_delete_to_wal(db_path, config_path):
    _downgrade_to_v83_delete(db_path)
    assert _journal_mode(db_path) == "delete"
    assert _user_version(db_path) == 83

    tusk_migrate.migrate_84(str(db_path), str(config_path), BIN_DIR)

    assert _journal_mode(db_path) == "wal"
    assert _user_version(db_path) == 84


def test_migrate_84_idempotent_on_already_wal(db_path, config_path):
    # Already WAL (fresh init), but stamp v83 so the migration body actually runs
    # its PRAGMA — confirming journal_mode=WAL is a harmless no-op on a WAL DB.
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA user_version = 83")
    finally:
        conn.close()
    assert _journal_mode(db_path) == "wal"

    tusk_migrate.migrate_84(str(db_path), str(config_path), BIN_DIR)

    assert _journal_mode(db_path) == "wal"
    assert _user_version(db_path) == 84

    # A second call hits the get_version >= 84 guard and is a no-op.
    tusk_migrate.migrate_84(str(db_path), str(config_path), BIN_DIR)
    assert _journal_mode(db_path) == "wal"
    assert _user_version(db_path) == 84


def test_fresh_init_is_wal_at_or_past_v84(db_path):
    # cmd_init enables WAL and stamps the latest schema version, so a fresh DB
    # never needs migration 84. (>= so this keeps passing when later migrations
    # land — see CLAUDE.md checklist.)
    assert _journal_mode(db_path) == "wal"
    assert _user_version(db_path) >= 84
