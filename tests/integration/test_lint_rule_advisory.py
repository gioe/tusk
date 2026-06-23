"""Integration test for migration 82: lint_rules.enforcement column (Task 711).

Selected by criterion 3322 via `-k migration`.
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


def _downgrade_to_v81(db_path) -> None:
    """Drop the enforcement column and stamp v81 to simulate a pre-82 DB.

    SQLite cannot DROP COLUMN on every supported version, so rebuild the table
    without the column.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS lint_rules;
            CREATE TABLE lint_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                grep_pattern TEXT NOT NULL,
                file_glob TEXT NOT NULL,
                message TEXT NOT NULL,
                is_blocking INTEGER NOT NULL DEFAULT 0,
                source_skill TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                CHECK (is_blocking IN (0, 1))
            );
            PRAGMA user_version = 81;
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


def test_migration_adds_enforcement_column(db_path, config_path):
    _downgrade_to_v81(db_path)
    assert "enforcement" not in _table_columns(db_path, "lint_rules")

    tusk_migrate.migrate_82(str(db_path), str(config_path), os.path.join(REPO_ROOT, "bin"))

    cols = _table_columns(db_path, "lint_rules")
    assert "enforcement" in cols, "migration 82 must add lint_rules.enforcement"

    conn = sqlite3.connect(str(db_path))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        # Default for new rows must be 'enforcing' (behavior unchanged for
        # rules that don't opt into the advisory tier).
        conn.execute(
            "INSERT INTO lint_rules (grep_pattern, file_glob, message)"
            " VALUES ('p', '**/*.py', 'm')"
        )
        conn.commit()
        enforcement = conn.execute(
            "SELECT enforcement FROM lint_rules ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert version == 82
    assert enforcement == "enforcing"


def test_migration_check_constraint_rejects_bad_enforcement(db_path, config_path):
    _downgrade_to_v81(db_path)
    tusk_migrate.migrate_82(str(db_path), str(config_path), os.path.join(REPO_ROOT, "bin"))

    conn = sqlite3.connect(str(db_path))
    try:
        # 'advisory' is accepted ...
        conn.execute(
            "INSERT INTO lint_rules (grep_pattern, file_glob, message, enforcement)"
            " VALUES ('p', '**/*.py', 'm', 'advisory')"
        )
        conn.commit()
        # ... an out-of-domain value is rejected by the CHECK constraint.
        import pytest

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO lint_rules (grep_pattern, file_glob, message, enforcement)"
                " VALUES ('p', '**/*.py', 'm', 'bogus')"
            )
            conn.commit()
    finally:
        conn.close()


def test_migration_fresh_init_at_or_past_v82(db_path):
    # cmd_init stamps the latest schema version; the column must exist on a
    # fresh DB without running the migration.
    cols = _table_columns(db_path, "lint_rules")
    assert "enforcement" in cols, "fresh init should create lint_rules.enforcement"
    conn = sqlite3.connect(str(db_path))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version >= 82


def test_migration_idempotent_when_already_at_v82(db_path, config_path):
    # Fresh DBs initialize at the latest schema; stamp 82 explicitly so this
    # test keeps passing when later migrations land (see CLAUDE.md checklist).
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA user_version = 82")
        conn.execute(
            "INSERT INTO lint_rules (grep_pattern, file_glob, message, enforcement)"
            " VALUES ('p', '**/*.py', 'm', 'advisory')"
        )
        conn.commit()
    finally:
        conn.close()

    # Re-running must not drop the column or lose the row.
    tusk_migrate.migrate_82(str(db_path), str(config_path), os.path.join(REPO_ROOT, "bin"))

    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM lint_rules WHERE enforcement = 'advisory'"
        ).fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert count == 1
    assert version >= 82
