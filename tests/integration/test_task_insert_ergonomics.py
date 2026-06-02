"""Integration coverage for task-insert CLI ergonomics."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _insert(db_path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "TUSK_DB": str(db_path)}
    return subprocess.run(
        [TUSK_BIN, "task-insert", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _task_row(db_path, task_id: int) -> tuple[str, str, str]:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT summary, description, priority FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row


def test_task_insert_accepts_description_flag_alias(db_path):
    result = _insert(
        db_path,
        "description alias smoke",
        "--description",
        "body text",
        "--priority",
        "Low",
        "--criteria",
        "done",
    )

    assert result.returncode == 0, result.stderr
    task_id = json.loads(result.stdout)["task_id"]

    assert _task_row(db_path, task_id) == (
        "description alias smoke",
        "body text",
        "Low",
    )


def test_task_insert_accepts_lowercase_priority_and_stores_canonical_value(db_path):
    result = _insert(
        db_path,
        "lowercase priority smoke",
        "body text",
        "--priority",
        "medium",
        "--criteria",
        "done",
    )

    assert result.returncode == 0, result.stderr
    task_id = json.loads(result.stdout)["task_id"]

    assert _task_row(db_path, task_id) == (
        "lowercase priority smoke",
        "body text",
        "Medium",
    )


def test_task_insert_preserves_title_case_priority(db_path):
    result = _insert(
        db_path,
        "title case priority smoke",
        "body text",
        "--priority",
        "High",
        "--criteria",
        "done",
    )

    assert result.returncode == 0, result.stderr
    task_id = json.loads(result.stdout)["task_id"]

    assert _task_row(db_path, task_id) == (
        "title case priority smoke",
        "body text",
        "High",
    )
