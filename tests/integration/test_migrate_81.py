"""Integration test for migration 81: precheck_verdicts table (issue #1083)."""

from __future__ import annotations

import importlib.util
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIGRATE_PATH = os.path.join(REPO_ROOT, "bin", "tusk-migrate.py")

_spec = importlib.util.spec_from_file_location("tusk_migrate", MIGRATE_PATH)
tusk_migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tusk_migrate)


def _downgrade_to_v80(db_path) -> None:
    """Drop precheck_verdicts and stamp v80 to simulate a pre-81 DB."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS precheck_verdicts;
            PRAGMA user_version = 80;
            """
        )
        conn.commit()
    finally:
        conn.close()


def _table_columns(db_path, table) -> dict:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return {r[1]: r[2] for r in rows}  # name -> declared type


def test_migrate_81_creates_precheck_verdicts(db_path, config_path):
    _downgrade_to_v80(db_path)

    tusk_migrate.migrate_81(str(db_path), str(config_path), os.path.join(REPO_ROOT, "bin"))

    cols = _table_columns(db_path, "precheck_verdicts")
    assert cols, "precheck_verdicts table should exist after migration 81"
    for expected in (
        "id", "task_id", "session_id", "head_sha",
        "test_command", "pre_existing", "exit_code", "created_at",
    ):
        assert expected in cols, f"missing column {expected}"

    # The lookup index must exist (drives the most-recent-verdict query).
    conn = sqlite3.connect(str(db_path))
    try:
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_precheck_verdicts_lookup'"
        ).fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert idx is not None, "expected idx_precheck_verdicts_lookup"
    assert version == 81


def test_fresh_init_at_or_past_v81(db_path):
    # cmd_init stamps the latest schema version so fresh installs never need
    # this migration; the table must be present on a fresh DB.
    cols = _table_columns(db_path, "precheck_verdicts")
    assert cols, "fresh init should create precheck_verdicts"
    conn = sqlite3.connect(str(db_path))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version >= 81


def test_idempotent_when_already_at_v81(db_path, config_path):
    # Fresh DBs initialize at the latest schema; stamp 81 explicitly so this
    # test keeps passing when later migrations land (see CLAUDE.md checklist).
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA user_version = 81")
        conn.execute(
            "INSERT INTO precheck_verdicts "
            "(head_sha, test_command, pre_existing, exit_code) "
            "VALUES ('abc123', 'pytest -q', 1, 1)"
        )
        conn.commit()
    finally:
        conn.close()

    # Re-running must not drop the table or lose the row.
    tusk_migrate.migrate_81(str(db_path), str(config_path), os.path.join(REPO_ROOT, "bin"))

    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM precheck_verdicts").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert count == 1
    assert version >= 81
