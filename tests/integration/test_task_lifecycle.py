"""Integration tests for tusk task lifecycle (start, done, criteria guards).

Tests the full lifecycle: insert a task, start it, complete criteria, close it.
Guard cases: closing with open criteria returns exit code 3 without --force;
closing with --force succeeds; wont_do closed_reason skips the commit-hash check;
already-Done task returns exit code 2. Invalid status transitions are rejected
by the DB trigger.
"""

import importlib.util
import io
import json
import os
import sqlite3
from contextlib import redirect_stderr, redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        os.path.join(REPO_ROOT, "bin", f"{name}.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_task_start = _load("tusk-task-start")
tusk_task_done = _load("tusk-task-done")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def insert_task(
    conn: sqlite3.Connection,
    summary: str,
    *,
    status: str = "To Do",
    closed_reason: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO tasks (summary, status, closed_reason, task_type, priority, complexity, priority_score)"
        " VALUES (?, ?, ?, 'feature', 'Medium', 'S', 50)",
        (summary, status, closed_reason),
    )
    conn.commit()
    return cur.lastrowid


def insert_criterion(
    conn: sqlite3.Connection,
    task_id: int,
    text: str,
    *,
    is_completed: int = 0,
    commit_hash: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO acceptance_criteria (task_id, criterion, source, is_completed, commit_hash)"
        " VALUES (?, ?, 'original', ?, ?)",
        (task_id, text, is_completed, commit_hash),
    )
    conn.commit()
    return cur.lastrowid


def call_start(db_path, config_path, task_id, *extra_args) -> tuple[int, dict | None, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_task_start.main([str(db_path), str(config_path), str(task_id), *extra_args])
    out = out_buf.getvalue().strip()
    result = json.loads(out) if out else None
    return rc, result, err_buf.getvalue()


def call_done(db_path, config_path, task_id, reason, *extra_args) -> tuple[int, dict | None, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_task_done.main(
            [str(db_path), str(config_path), str(task_id), "--reason", reason, *extra_args]
        )
    out = out_buf.getvalue().strip()
    result = json.loads(out) if out else None
    return rc, result, err_buf.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTaskLifecycle:

    def test_happy_path_full_lifecycle(self, db_path, config_path):
        """CID 1524: insert -> start -> criteria done -> close succeeds end-to-end."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Happy path task")
            cid = insert_criterion(conn, task_id, "Write the feature")
        finally:
            conn.close()

        # Start the task
        rc, result, _ = call_start(db_path, config_path, task_id)
        assert rc == 0
        assert result["task"]["status"] == "In Progress"
        assert result["session_id"] is not None

        # Mark criterion done with a commit hash
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute(
                "UPDATE acceptance_criteria SET is_completed = 1, commit_hash = 'abc1234'"
                " WHERE id = ?",
                (cid,),
            )
            conn.commit()
        finally:
            conn.close()

        # Close the task
        rc, result, _ = call_done(db_path, config_path, task_id, "completed")
        assert rc == 0
        assert result["task"]["status"] == "Done"
        assert result["task"]["closed_reason"] == "completed"

    def test_open_criteria_blocks_closure_exit_code_3(self, db_path, config_path):
        """CID 1525: closing with open criteria returns exit code 3 and stderr message."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Task with open criterion")
            insert_criterion(conn, task_id, "Still pending criterion")
            # Manually set to In Progress so task-done doesn't fail on status check
            conn.execute(
                "UPDATE tasks SET status = 'In Progress' WHERE id = ?", (task_id,)
            )
            conn.commit()
        finally:
            conn.close()

        rc, result, stderr = call_done(db_path, config_path, task_id, "completed")

        assert rc == 3
        assert result is None
        assert "uncompleted acceptance criteria" in stderr
        assert "--force" in stderr

    def test_force_flag_closes_task_with_open_criteria(self, db_path, config_path):
        """CID 1526: --force closes task even with open criteria."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Force close task")
            insert_criterion(conn, task_id, "Open criterion")
            conn.execute(
                "UPDATE tasks SET status = 'In Progress' WHERE id = ?", (task_id,)
            )
            conn.commit()
        finally:
            conn.close()

        rc, result, _ = call_done(db_path, config_path, task_id, "completed", "--force")

        assert rc == 0
        assert result["task"]["status"] == "Done"
        assert result["task"]["closed_reason"] == "completed"

    def test_wont_do_skips_commit_hash_check(self, db_path, config_path):
        """CID 1527: wont_do closure succeeds even when completed criteria lack a commit hash."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Wont do task")
            # Criterion is completed but has no commit_hash — would block 'completed' reason
            insert_criterion(conn, task_id, "Done but uncommitted", is_completed=1, commit_hash=None)
            conn.execute(
                "UPDATE tasks SET status = 'In Progress' WHERE id = ?", (task_id,)
            )
            conn.commit()
        finally:
            conn.close()

        rc, result, _ = call_done(db_path, config_path, task_id, "wont_do")

        assert rc == 0
        assert result["task"]["closed_reason"] == "wont_do"

    def test_already_done_task_returns_exit_code_2(self, db_path, config_path):
        """CID 1528: calling task-done on an already-Done task returns exit code 2."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Already done task", status="Done", closed_reason="completed")
        finally:
            conn.close()

        rc, result, stderr = call_done(db_path, config_path, task_id, "completed")

        assert rc == 2
        assert result is None
        assert "already Done" in stderr

    def test_invalid_status_transition_rejected_by_trigger(self, db_path, config_path):
        """CID 1529: DB trigger blocks invalid transitions (e.g. In Progress -> To Do)."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Trigger test task")
            insert_criterion(conn, task_id, "Some criterion")
            # Advance to In Progress (valid transition)
            conn.execute(
                "UPDATE tasks SET status = 'In Progress' WHERE id = ?", (task_id,)
            )
            conn.commit()

            # Attempt invalid transition: In Progress -> To Do
            with pytest.raises(sqlite3.IntegrityError, match="Invalid status transition"):
                conn.execute(
                    "UPDATE tasks SET status = 'To Do' WHERE id = ?", (task_id,)
                )
                conn.commit()
        finally:
            conn.close()
