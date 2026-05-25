"""Integration test for migrate_71: add plans table (issue #873)."""

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


def test_migrate_71_creates_plans_table(db_path, config_path):
    """Migration 71 must create the plans table with the documented columns
    and stamp user_version=71. Pre-migration data is preserved (table did
    not exist, so nothing to preserve — just confirm the migration is a
    no-touch on unrelated tables)."""
    db = str(db_path)
    # Drop the table to simulate a pre-migration DB even when fresh-init already
    # carries it.
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE IF EXISTS plans")
    conn.execute("DROP INDEX IF EXISTS idx_plans_name_effective")
    # Insert an unrelated row to confirm migration doesn't disturb other tables.
    conn.execute("INSERT INTO tasks (summary) VALUES ('seed task')")
    conn.execute("PRAGMA user_version = 70")
    conn.commit()
    conn.close()

    tusk_migrate.migrate_71(db, config_path, SCRIPT_DIR)

    assert tusk_migrate.get_version(db) == 71
    cols = _columns(db, "plans")
    assert cols == [
        "id",
        "name",
        "monthly_cost_dollars",
        "effective_from",
        "effective_to",
        "notes",
        "created_at",
    ]

    # Unrelated rows survive.
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT summary FROM tasks WHERE id = 1").fetchone()
    finally:
        conn.close()
    assert row[0] == "seed task"


def test_migrate_71_check_constraint_rejects_negative_cost(db_path, config_path):
    """The CHECK (monthly_cost_dollars >= 0) constraint must block negative
    values — a subscription cost cannot be negative."""
    db = str(db_path)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE IF EXISTS plans")
    conn.execute("PRAGMA user_version = 70")
    conn.commit()
    conn.close()

    tusk_migrate.migrate_71(db, config_path, SCRIPT_DIR)

    conn = sqlite3.connect(db)
    try:
        import pytest
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO plans (name, monthly_cost_dollars, effective_from)"
                " VALUES ('test', -10.0, '2026-01-01')"
            )
    finally:
        conn.close()


def test_migrate_71_idempotent_when_already_at_v71(db_path, config_path):
    """Running migration 71 against a DB already stamped to v71 is a no-op."""
    db = str(db_path)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 71")
    conn.commit()
    conn.close()

    tusk_migrate.migrate_71(db, config_path, SCRIPT_DIR)

    assert tusk_migrate.get_version(db) >= 71
