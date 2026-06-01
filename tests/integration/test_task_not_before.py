"""Integration coverage for tasks.not_before time gating."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [TUSK_BIN, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _insert_task(
    conn: sqlite3.Connection,
    summary: str,
    *,
    priority_score: int = 50,
    not_before: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO tasks
            (summary, description, status, priority, task_type, complexity,
             priority_score, not_before)
        VALUES (?, 'body', 'To Do', 'Medium', 'feature', 'S', ?, ?)
        """,
        (summary, priority_score, not_before),
    )
    task_id = cur.lastrowid
    conn.execute(
        "INSERT INTO acceptance_criteria (task_id, criterion, source, is_completed) "
        "VALUES (?, 'done', 'original', 0)",
        (task_id,),
    )
    conn.commit()
    return task_id


def test_task_insert_accepts_relative_not_before(db_path):
    result = _run(
        "task-insert",
        "future task",
        "body",
        "--criteria",
        "done",
        "--not-before",
        "+4h",
    )

    assert result.returncode == 0, result.stderr
    task_id = json.loads(result.stdout)["task_id"]

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT not_before, datetime(not_before) > datetime('now'), "
            "datetime(not_before) <= datetime('now', '+5 hours') "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0]
    assert row[1] == 1
    assert row[2] == 1


def test_task_select_skips_future_not_before_task(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        future_id = _insert_task(
            conn,
            "future high-priority task",
            priority_score=100,
            not_before="2999-01-01 00:00:00",
        )
        ready_id = _insert_task(conn, "ready lower-priority task", priority_score=20)
    finally:
        conn.close()

    result = _run("task-select")

    assert result.returncode == 0, result.stderr
    selected = json.loads(result.stdout)
    assert selected["id"] == ready_id
    assert selected["id"] != future_id


def test_task_start_refuses_future_not_before_without_override(db_path, config_path):
    conn = sqlite3.connect(str(db_path))
    try:
        task_id = _insert_task(
            conn,
            "future explicit task",
            not_before="2999-01-01 00:00:00",
        )
    finally:
        conn.close()

    result = _run("task-start", str(task_id))

    assert result.returncode == 2
    assert "deferred until 2999-01-01 00:00:00" in result.stderr
    assert "--force-not-before" in result.stderr

    forced = _run("task-start", str(task_id), "--force-not-before")

    assert forced.returncode == 0, forced.stderr
    payload = json.loads(forced.stdout)
    assert payload["task"]["id"] == task_id
    assert payload["task"]["status"] == "In Progress"
    assert "Proceeding anyway due to --force-not-before" in forced.stderr


def test_task_list_exposes_not_before_in_text_and_json(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        task_id = _insert_task(
            conn,
            "future visible task",
            not_before="2999-01-01 00:00:00",
        )
    finally:
        conn.close()

    json_result = _run("task-list", "--format", "json")
    assert json_result.returncode == 0, json_result.stderr
    rows = json.loads(json_result.stdout)
    row = next(r for r in rows if r["id"] == task_id)
    assert row["not_before"] == "2999-01-01 00:00:00"

    text_result = _run("task-list")
    assert text_result.returncode == 0, text_result.stderr
    assert "NOT_BEFORE" in text_result.stdout
    assert "2999-01-01 00:00:00" in text_result.stdout
