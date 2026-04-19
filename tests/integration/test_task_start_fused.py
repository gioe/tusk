"""Integration tests for the fused tusk-task-start no-ID path.

When invoked without a task_id argument, tusk-task-start picks the top
WSJF-ranked ready task from v_ready_tasks and starts it in one call —
eliminating the select+start round-trip the /tusk no-arg path used to pay.
These tests lock the fused-path behavior (top pick, empty-backlog exit,
blocked-task skipping, prerequisite-warning preservation) and verify that
the explicit <task_id> path is unchanged.
"""

import importlib.util
import io
import json
import os
import sqlite3
from contextlib import redirect_stderr, redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_task_start",
    os.path.join(REPO_ROOT, "bin", "tusk-task-start.py"),
)
tusk_task_start = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tusk_task_start)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def insert_task(
    conn: sqlite3.Connection,
    summary: str,
    *,
    status: str = "To Do",
    priority_score: int = 60,
    complexity: str = "S",
    description: str = "",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO tasks (summary, status, priority, complexity, task_type,
                           priority_score, description)
        VALUES (?, ?, 'Medium', ?, 'feature', ?, ?)
        """,
        (summary, status, complexity, priority_score, description),
    )
    conn.commit()
    return cur.lastrowid


def insert_criterion(conn: sqlite3.Connection, task_id: int, text: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO acceptance_criteria (task_id, criterion, source, is_completed)
        VALUES (?, ?, 'original', 0)
        """,
        (task_id, text),
    )
    conn.commit()
    return cur.lastrowid


def add_blocking_dep(conn: sqlite3.Connection, task_id: int, depends_on_id: int) -> None:
    conn.execute(
        """
        INSERT INTO task_dependencies (task_id, depends_on_id, relationship_type)
        VALUES (?, ?, 'blocks')
        """,
        (task_id, depends_on_id),
    )
    conn.commit()


def call_start(db_path, config_path, *extra_args) -> tuple[int, dict | None, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_task_start.main([str(db_path), str(config_path), *extra_args])
    out = out_buf.getvalue().strip()
    result = json.loads(out) if out else None
    return rc, result, err_buf.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFusedTaskStart:
    def test_no_id_starts_top_wsjf_ready_task(self, db_path, config_path):
        """CID 425: task-start with no ID picks the top WSJF-ranked ready task and starts it."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            low_id = insert_task(conn, "Low priority task", priority_score=20)
            insert_criterion(conn, low_id, "do the low thing")
            high_id = insert_task(conn, "High priority task", priority_score=90)
            insert_criterion(conn, high_id, "do the high thing")
            mid_id = insert_task(conn, "Medium priority task", priority_score=50)
            insert_criterion(conn, mid_id, "do the mid thing")
        finally:
            conn.close()

        rc, result, _ = call_start(db_path, config_path, "--force")

        assert rc == 0
        assert result is not None
        assert result["task"]["id"] == high_id
        assert result["task"]["summary"] == "High priority task"
        assert result["task"]["status"] == "In Progress"
        assert result["session_id"] is not None

    def test_no_id_exits_1_when_backlog_empty(self, db_path, config_path):
        """CID 429: exit 1 with the canonical empty-backlog stderr when no ready tasks exist."""
        rc, result, stderr = call_start(db_path, config_path, "--force")

        assert rc == 1
        assert result is None
        assert "No ready tasks found" in stderr

    def test_no_id_skips_blocked_tasks(self, db_path, config_path):
        """CID 425: fused path respects v_ready_tasks — a blocked high-priority task is skipped."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            blocker_id = insert_task(conn, "Blocker task", priority_score=40)
            insert_criterion(conn, blocker_id, "unblock")
            blocked_id = insert_task(conn, "Blocked high-priority task", priority_score=100)
            insert_criterion(conn, blocked_id, "do the blocked thing")
            add_blocking_dep(conn, blocked_id, blocker_id)
        finally:
            conn.close()

        rc, result, _ = call_start(db_path, config_path, "--force")

        assert rc == 0
        assert result is not None
        assert result["task"]["id"] == blocker_id
        assert result["task"]["status"] == "In Progress"

    def test_no_id_preserves_prerequisite_warning(self, db_path, config_path):
        """CID 428: the 'references unfinished prerequisite tasks' stderr warning
        still fires when the selected-via-fused-path task references a To Do TASK-NNN."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            prereq_id = insert_task(conn, "Prerequisite task", priority_score=30)
            insert_criterion(conn, prereq_id, "do the prereq")
            main_id = insert_task(
                conn,
                "Main task",
                priority_score=100,
                description=f"Depends on TASK-{prereq_id} being finished first.",
            )
            insert_criterion(conn, main_id, "do the main thing")
        finally:
            conn.close()

        rc, result, stderr = call_start(db_path, config_path, "--force")

        assert rc == 0
        assert result is not None
        assert result["task"]["id"] == main_id
        assert "Warning" in stderr
        assert f"TASK-{prereq_id}" in stderr
        assert "Prerequisite task" in stderr

    def test_explicit_task_id_path_unchanged(self, db_path, config_path):
        """CID 434(c): passing an explicit task_id still starts that specific task
        (regression guard — the fused path only activates when task_id is omitted)."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            # Insert a high-priority task AND a lower-priority explicit target.
            insert_task(conn, "High priority task not chosen", priority_score=100)
            target_id = insert_task(conn, "Explicit target", priority_score=20)
            insert_criterion(conn, target_id, "do the target thing")
        finally:
            conn.close()

        rc, result, _ = call_start(db_path, config_path, str(target_id), "--force")

        assert rc == 0
        assert result is not None
        assert result["task"]["id"] == target_id
        assert result["task"]["summary"] == "Explicit target"
        assert result["task"]["status"] == "In Progress"
