"""Unit tests for tusk-context.py."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_context",
    os.path.join(BIN, "tusk-context.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT
);
CREATE TABLE objectives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL
);
CREATE TABLE task_context_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    objective_id INTEGER,
    item_type TEXT NOT NULL CHECK (item_type IN ('memory', 'assumption', 'question', 'risk', 'decision', 'entry_point')),
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'resolved', 'superseded')),
    source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'create_task', 'task_progress', 'review', 'retro', 'agent_handoff')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (objective_id) REFERENCES objectives(id) ON DELETE SET NULL
);
"""


def _make_db(tmp_path):
    db_path = str(tmp_path / "context.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO tasks (id, summary) VALUES (42, 'parent task')")
    conn.execute("INSERT INTO objectives (id, summary) VALUES (7, 'larger intent')")
    conn.commit()
    return db_path, conn


def _run_cli(db_path, *cli_args, config_path="fake.json"):
    return subprocess.run(
        [sys.executable, os.path.join(BIN, "tusk-context.py"),
         db_path, config_path, *cli_args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_add_creates_active_context_item_with_optional_objective(tmp_path):
    db_path, _ = _make_db(tmp_path)

    result = _run_cli(
        db_path,
        "add",
        "TASK-42",
        "--type",
        "assumption",
        "--content",
        "Use the durable DB packet.",
        "--source",
        "create_task",
        "--objective-id",
        "7",
    )

    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout)
    assert row["task_id"] == 42
    assert row["objective_id"] == 7
    assert row["item_type"] == "assumption"
    assert row["status"] == "active"
    assert row["source"] == "create_task"


def test_list_filters_active_items_by_type_and_status(tmp_path):
    db_path, conn = _make_db(tmp_path)
    conn.execute(
        "INSERT INTO task_context_items (task_id, item_type, content) "
        "VALUES (42, 'risk', 'Risk A')"
    )
    conn.execute(
        "INSERT INTO task_context_items (task_id, item_type, content, status) "
        "VALUES (42, 'risk', 'Risk B', 'resolved')"
    )
    conn.execute(
        "INSERT INTO task_context_items (task_id, item_type, content) "
        "VALUES (42, 'memory', 'Memory A')"
    )
    conn.commit()

    result = _run_cli(db_path, "list", "42", "--type", "risk")

    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert [r["content"] for r in rows] == ["Risk A"]

    result_all = _run_cli(db_path, "list", "42", "--type", "risk", "--status", "all")
    assert result_all.returncode == 0, result_all.stderr
    rows_all = json.loads(result_all.stdout)
    assert [r["content"] for r in rows_all] == ["Risk A", "Risk B"]


def test_list_text_format_is_human_readable(tmp_path):
    db_path, conn = _make_db(tmp_path)
    conn.execute(
        "INSERT INTO task_context_items (task_id, item_type, content, source) "
        "VALUES (42, 'decision', 'Keep review notes as context.', 'review')"
    )
    conn.commit()

    result = _run_cli(db_path, "list", "42", "--format", "text")

    assert result.returncode == 0, result.stderr
    assert "decision" in result.stdout
    assert "Keep review notes as context." in result.stdout


def test_resolve_and_supersede_preserve_row_with_resolved_at(tmp_path):
    db_path, conn = _make_db(tmp_path)
    item_id = conn.execute(
        "INSERT INTO task_context_items (task_id, item_type, content) "
        "VALUES (42, 'question', 'Which path owns hydration?')"
    ).lastrowid
    other_id = conn.execute(
        "INSERT INTO task_context_items (task_id, item_type, content) "
        "VALUES (42, 'memory', 'Old note')"
    ).lastrowid
    conn.commit()

    resolved = _run_cli(db_path, "resolve", str(item_id))
    superseded = _run_cli(db_path, "supersede", str(other_id))

    assert resolved.returncode == 0, resolved.stderr
    assert superseded.returncode == 0, superseded.stderr
    resolved_row = json.loads(resolved.stdout)
    superseded_row = json.loads(superseded.stdout)
    assert resolved_row["status"] == "resolved"
    assert resolved_row["resolved_at"] is not None
    assert superseded_row["status"] == "superseded"
    assert superseded_row["resolved_at"] is not None
    assert conn.execute("SELECT COUNT(*) FROM task_context_items").fetchone()[0] == 2


def test_invalid_task_objective_and_empty_content_fail(tmp_path):
    db_path, _ = _make_db(tmp_path)

    missing_task = _run_cli(
        db_path, "add", "999", "--type", "memory", "--content", "x"
    )
    missing_objective = _run_cli(
        db_path,
        "add",
        "42",
        "--type",
        "memory",
        "--content",
        "x",
        "--objective-id",
        "999",
    )
    empty_content = _run_cli(
        db_path, "add", "42", "--type", "memory", "--content", "   "
    )

    assert missing_task.returncode == 1
    assert "task 999 not found" in missing_task.stderr
    assert missing_objective.returncode == 1
    assert "objective 999 not found" in missing_objective.stderr
    assert empty_content.returncode == 1
    assert "--content must not be empty" in empty_content.stderr


def test_argparse_rejects_invalid_type_and_status(tmp_path):
    db_path, _ = _make_db(tmp_path)

    bad_type = _run_cli(db_path, "add", "42", "--type", "idea", "--content", "x")
    bad_status = _run_cli(db_path, "list", "42", "--status", "closed")

    assert bad_type.returncode == 2
    assert "invalid choice" in bad_type.stderr
    assert bad_status.returncode == 2
    assert "invalid choice" in bad_status.stderr
