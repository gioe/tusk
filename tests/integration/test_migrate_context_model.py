"""Integration test for migration 77: objective/context handoff model."""

from __future__ import annotations

import importlib.util
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT_DIR = os.path.join(REPO_ROOT, "bin")
MIGRATE_PATH = os.path.join(SCRIPT_DIR, "tusk-migrate.py")

_spec = importlib.util.spec_from_file_location("tusk_migrate", MIGRATE_PATH)
tusk_migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tusk_migrate)


def _drop_context_model_tables(db_path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS task_context_items;
            DROP TABLE IF EXISTS objective_tasks;
            DROP TABLE IF EXISTS objectives;
            PRAGMA user_version = 76;
            """
        )
        conn.commit()
    finally:
        conn.close()


def _columns(db_path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def test_migrate_77_adds_context_model_tables(db_path, config_path):
    _drop_context_model_tables(db_path)

    tusk_migrate.migrate_77(str(db_path), str(config_path), SCRIPT_DIR)

    assert _columns(db_path, "objectives") == {
        "id",
        "summary",
        "description",
        "status",
        "created_at",
        "updated_at",
        "closed_at",
    }
    assert _columns(db_path, "objective_tasks") == {
        "objective_id",
        "task_id",
        "relationship_type",
        "created_at",
    }
    assert _columns(db_path, "task_context_items") == {
        "id",
        "task_id",
        "objective_id",
        "item_type",
        "content",
        "status",
        "source",
        "created_at",
        "updated_at",
        "resolved_at",
    }

    conn = sqlite3.connect(str(db_path))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        task_id = conn.execute(
            "INSERT INTO tasks (summary, status, priority_score) VALUES ('handoff task', 'To Do', 10)"
        ).lastrowid
        objective_id = conn.execute(
            "INSERT INTO objectives (summary, description) VALUES ('ship context model', 'larger intent')"
        ).lastrowid
        conn.execute(
            "INSERT INTO objective_tasks (objective_id, task_id, relationship_type) VALUES (?, ?, 'primary')",
            (objective_id, task_id),
        )
        conn.execute(
            """
            INSERT INTO task_context_items (task_id, objective_id, item_type, content, source)
            VALUES (?, ?, 'assumption', 'Tasks remain the shippable unit.', 'create_task')
            """,
            (task_id, objective_id),
        )
        conn.commit()

        context_row = conn.execute(
            """
            SELECT tci.item_type, tci.status, tci.source, ot.relationship_type
              FROM task_context_items tci
              JOIN objective_tasks ot ON ot.objective_id = tci.objective_id
             WHERE tci.task_id = ?
            """,
            (task_id,),
        ).fetchone()
    finally:
        conn.close()

    assert version == 77
    assert context_row == ("assumption", "active", "create_task", "primary")


def test_migrate_77_is_idempotent_when_already_at_v77(db_path, config_path):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA user_version = 77")
        conn.commit()
        version_before = conn.execute("PRAGMA user_version").fetchone()[0]
        objectives_before = conn.execute("SELECT COUNT(*) FROM objectives").fetchone()[0]
    finally:
        conn.close()

    tusk_migrate.migrate_77(str(db_path), str(config_path), SCRIPT_DIR)

    conn = sqlite3.connect(str(db_path))
    try:
        version_after = conn.execute("PRAGMA user_version").fetchone()[0]
        objectives_after = conn.execute("SELECT COUNT(*) FROM objectives").fetchone()[0]
    finally:
        conn.close()

    assert version_after == version_before
    assert objectives_after == objectives_before
