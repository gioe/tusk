"""Integration tests for tusk-task-select.py.

Uses the db_path fixture (a real initialised SQLite DB) and calls
tusk_task_select.main() directly with various flag combinations.
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
    "tusk_task_select",
    os.path.join(REPO_ROOT, "bin", "tusk-task-select.py"),
)
tusk_task_select = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tusk_task_select)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def insert_task(
    conn: sqlite3.Connection,
    summary: str,
    *,
    status: str = "To Do",
    priority: str = "Medium",
    complexity: str = "S",
    priority_score: int = 60,
    task_type: str = "feature",
    description: str = "",
) -> int:
    """Insert a task row and return its id."""
    cur = conn.execute(
        """
        INSERT INTO tasks (summary, status, priority, complexity, task_type, priority_score, description)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (summary, status, priority, complexity, task_type, priority_score, description),
    )
    conn.commit()
    return cur.lastrowid


def add_blocking_dep(conn: sqlite3.Connection, task_id: int, depends_on_id: int) -> None:
    """task_id is blocked by depends_on_id (blocks relationship)."""
    conn.execute(
        """
        INSERT INTO task_dependencies (task_id, depends_on_id, relationship_type)
        VALUES (?, ?, 'blocks')
        """,
        (task_id, depends_on_id),
    )
    conn.commit()


def call_main(db_path, config_path, *extra_args) -> tuple[int, dict | None]:
    """Call main() and return (exit_code, parsed_json_or_None)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = tusk_task_select.main([str(db_path), str(config_path), *extra_args])
    output = buf.getvalue().strip()
    result = json.loads(output) if output else None
    return rc, result


def call_main_with_stderr(db_path, config_path, *extra_args) -> tuple[int, dict | None, str]:
    """Call main() and return (exit_code, parsed_json_or_None, stderr_output)."""
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        rc = tusk_task_select.main([str(db_path), str(config_path), *extra_args])
    output = stdout_buf.getvalue().strip()
    result = json.loads(output) if output else None
    return rc, result, stderr_buf.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTaskSelect:
    def test_top_wsjf_task_selected_with_no_filters(self, db_path, config_path):
        """CID 1518: highest priority_score task is returned when no filters applied."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            insert_task(conn, "Low priority task", priority_score=20)
            insert_task(conn, "High priority task", priority_score=90)
            insert_task(conn, "Medium priority task", priority_score=50)
        finally:
            conn.close()

        rc, result = call_main(db_path, config_path)

        assert rc == 0
        assert result is not None
        assert result["summary"] == "High priority task"
        assert result["priority_score"] == 90

    def test_max_complexity_excludes_tasks_above_cap(self, db_path, config_path):
        """CID 1519: tasks with complexity above --max-complexity are excluded."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            # XL task has highest priority_score but should be filtered out
            insert_task(conn, "XL task", complexity="XL", priority_score=100)
            insert_task(conn, "L task", complexity="L", priority_score=80)
            insert_task(conn, "S task", complexity="S", priority_score=40)
        finally:
            conn.close()

        rc, result = call_main(db_path, config_path, "--max-complexity", "S")

        assert rc == 0
        assert result is not None
        assert result["summary"] == "S task"
        assert result["complexity"] == "S"

    def test_max_complexity_includes_all_tiers_at_or_below_cap(self, db_path, config_path):
        """CID 1519 (extended): all tiers at or below the cap are eligible."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            insert_task(conn, "XS task", complexity="XS", priority_score=55)
            insert_task(conn, "S task", complexity="S", priority_score=70)
            insert_task(conn, "M task", complexity="M", priority_score=100)  # excluded
        finally:
            conn.close()

        rc, result = call_main(db_path, config_path, "--max-complexity", "S")

        assert rc == 0
        assert result is not None
        assert result["summary"] == "S task"

    def test_exclude_ids_skips_specified_tasks(self, db_path, config_path):
        """CID 1520: tasks listed in --exclude-ids are not returned."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            top_id = insert_task(conn, "Top task", priority_score=100)
            insert_task(conn, "Second task", priority_score=60)
        finally:
            conn.close()

        rc, result = call_main(db_path, config_path, "--exclude-ids", str(top_id))

        assert rc == 0
        assert result is not None
        assert result["summary"] == "Second task"
        assert result["id"] != top_id

    def test_blocked_tasks_do_not_appear_in_results(self, db_path, config_path):
        """CID 1521: tasks with an incomplete blocking dependency are excluded."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            blocker_id = insert_task(conn, "Blocker task", priority_score=40)
            blocked_id = insert_task(conn, "Blocked task", priority_score=100)
            # blocked_task depends_on blocker_task (blocker_task blocks blocked_task)
            add_blocking_dep(conn, blocked_id, blocker_id)
        finally:
            conn.close()

        rc, result = call_main(db_path, config_path)

        assert rc == 0
        assert result is not None
        # The blocked task (highest score) should be skipped; blocker task returned
        assert result["id"] == blocker_id
        assert result["summary"] == "Blocker task"

    def test_exit_code_1_when_no_ready_tasks(self, db_path, config_path):
        """CID 1522: exit code 1 when no tasks match filters."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            insert_task(conn, "Only XL task", complexity="XL", priority_score=100)
        finally:
            conn.close()

        rc, result = call_main(db_path, config_path, "--max-complexity", "XS")

        assert rc == 1
        assert result is None

    def test_exit_code_1_when_backlog_empty(self, db_path, config_path):
        """CID 1522 (extended): exit code 1 when no tasks exist at all."""
        rc, result = call_main(db_path, config_path)

        assert rc == 1
        assert result is None

    def test_exclude_ids_multiple_tasks_skipped(self, db_path, config_path):
        """CID 1520 (extended): multiple IDs in --exclude-ids are all skipped."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            id1 = insert_task(conn, "Task A", priority_score=100)
            id2 = insert_task(conn, "Task B", priority_score=80)
            insert_task(conn, "Task C", priority_score=40)
        finally:
            conn.close()

        rc, result = call_main(db_path, config_path, "--exclude-ids", f"{id1},{id2}")

        assert rc == 0
        assert result is not None
        assert result["summary"] == "Task C"

    def test_warns_on_stderr_when_referenced_task_is_todo(self, db_path, config_path):
        """CID 167/168/169: warning printed to stderr when description references a To Do task."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            prereq_id = insert_task(conn, "Prerequisite task", priority_score=30)
            insert_task(
                conn,
                "Main task",
                description=f"This task requires TASK-{prereq_id} to be completed first.",
                priority_score=100,
            )
        finally:
            conn.close()

        rc, result, stderr = call_main_with_stderr(db_path, config_path)

        assert rc == 0
        assert result is not None
        assert result["summary"] == "Main task"
        assert "Warning" in stderr
        assert f"TASK-{prereq_id}" in stderr
        assert "Prerequisite task" in stderr

    def test_no_warning_when_referenced_task_is_in_progress(self, db_path, config_path):
        """CID 170: no warning when the referenced task is In Progress (not To Do)."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            prereq_id = insert_task(conn, "In-progress prereq", status="In Progress", priority_score=30)
            insert_task(
                conn,
                "Main task",
                description=f"Depends on TASK-{prereq_id}.",
                priority_score=100,
            )
        finally:
            conn.close()

        rc, result, stderr = call_main_with_stderr(db_path, config_path)

        assert rc == 0
        assert result is not None
        assert "Warning" not in stderr

    def test_no_warning_when_no_task_references_in_description(self, db_path, config_path):
        """CID 171: no warning when the selected task has no TASK-NNN references."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            insert_task(conn, "Standalone task", description="No references here.", priority_score=100)
        finally:
            conn.close()

        rc, result, stderr = call_main_with_stderr(db_path, config_path)

        assert rc == 0
        assert result is not None
        assert "Warning" not in stderr
