"""Integration test for migrate_68: add task_workspaces table."""

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


def _indexes(db, table):
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    finally:
        conn.close()
    return sorted(r[1] for r in rows)


def _fk_list(db, table):
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    finally:
        conn.close()
    return {r[2]: r[6] for r in rows}


def test_migrate_68_adds_task_workspaces_table(db_path, config_path):
    db = str(db_path)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE IF EXISTS task_workspaces")
    conn.execute("PRAGMA user_version = 67")
    conn.commit()
    conn.close()

    tusk_migrate.migrate_68(db, config_path, SCRIPT_DIR)

    assert tusk_migrate.get_version(db) == 68
    assert _columns(db, "task_workspaces") == [
        "id",
        "task_id",
        "branch",
        "workspace_path",
        "created_at",
        "updated_at",
    ]
    assert "idx_task_workspaces_task_id" in _indexes(db, "task_workspaces")
    assert _fk_list(db, "task_workspaces") == {"tasks": "CASCADE"}


def test_migrate_68_idempotent_when_already_at_v68(db_path, config_path):
    db = str(db_path)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 68")
    conn.commit()
    conn.close()

    tusk_migrate.migrate_68(db, config_path, SCRIPT_DIR)

    assert tusk_migrate.get_version(db) >= 68

