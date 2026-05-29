"""Integration tests for direction-aware prerequisite warnings in task-start.

Issue #956: `tusk task-start` emitted an inverted dependency-direction warning.
The "references unfinished prerequisite tasks" stderr warning is driven by a
text scan of the started task's own description/summary for TASK-N mentions. A
mention does not imply direction, so a referenced *downstream dependent* (one
that `depends_on` the task being started) was wrongly labeled a prerequisite.

The fix filters the text-scan results through `task_dependencies`: a referenced
To Do task is dropped from the warning when it `depends_on` the current task via
`blocks` (the inverted case). Genuine prerequisites — and un-formalized text
references with no dependency row — are preserved.
"""

import importlib.util
import io
import json
import os
import sqlite3
from contextlib import redirect_stderr, redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_task_start",
    os.path.join(REPO_ROOT, "bin", "tusk-task-start.py"),
)
tusk_task_start = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tusk_task_start)

_WARN_HEADER = "references unfinished prerequisite tasks"


def _insert_task(
    conn: sqlite3.Connection,
    summary: str,
    *,
    description: str = "",
    status: str = "To Do",
) -> int:
    cur = conn.execute(
        "INSERT INTO tasks (summary, description, status, task_type, priority,"
        " complexity, priority_score) VALUES (?, ?, ?, 'feature', 'Medium', 'S', 50)",
        (summary, description, status),
    )
    conn.commit()
    return cur.lastrowid


def _insert_criterion(conn: sqlite3.Connection, task_id: int, text: str) -> int:
    cur = conn.execute(
        "INSERT INTO acceptance_criteria (task_id, criterion, source, is_completed)"
        " VALUES (?, ?, 'original', 0)",
        (task_id, text),
    )
    conn.commit()
    return cur.lastrowid


def _add_dep(
    conn: sqlite3.Connection,
    task_id: int,
    depends_on_id: int,
    rel: str = "blocks",
) -> None:
    conn.execute(
        "INSERT INTO task_dependencies (task_id, depends_on_id, relationship_type)"
        " VALUES (?, ?, ?)",
        (task_id, depends_on_id, rel),
    )
    conn.commit()


def _call_start(db_path, config_path, *extra_args) -> tuple[int, dict | None, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_task_start.main([str(db_path), str(config_path), *extra_args])
    out = out_buf.getvalue().strip()
    result = json.loads(out) if out else None
    return rc, result, err_buf.getvalue()


class TestPrereqWarningDirection:
    def test_inverted_direction_does_not_warn(self, db_path, config_path):
        """Starting prerequisite A (where B depends_on A via blocks) must NOT
        warn that B is A's prerequisite, even when A's text mentions TASK-B.

        This is the issue #956 incident: dep 2510 depends_on 2514 (blocks);
        `tusk task-start 2514` wrongly warned about TASK-2510.
        """
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            prereq = _insert_task(conn, "prerequisite task A")
            dependent = _insert_task(conn, "downstream dependent B")
            # A's text mentions B; B depends_on A via blocks.
            conn.execute(
                "UPDATE tasks SET description = ? WHERE id = ?",
                (f"Follow-up work is tracked in TASK-{dependent}.", prereq),
            )
            conn.commit()
            _insert_criterion(conn, prereq, "c1")
            _insert_criterion(conn, dependent, "c1")
            _add_dep(conn, dependent, prereq, "blocks")
        finally:
            conn.close()

        rc, result, stderr = _call_start(db_path, config_path, str(prereq))

        assert rc == 0, stderr
        assert result is not None
        assert result["task"]["id"] == prereq
        # The downstream dependent must not be labeled a prerequisite.
        assert _WARN_HEADER not in stderr
        assert f"TASK-{dependent}" not in stderr

    def test_true_prerequisite_still_warns(self, db_path, config_path):
        """Starting A which genuinely depends_on an unfinished P via blocks must
        still warn that P is an unfinished prerequisite when A's text mentions P.

        A real blocks dep makes the dep guard refuse the start, so --force-deps
        is required to reach the text-scan warning. The warning must name P.
        """
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            prereq = _insert_task(conn, "upstream prerequisite P")
            dependent = _insert_task(conn, "dependent task A")
            conn.execute(
                "UPDATE tasks SET description = ? WHERE id = ?",
                (f"Blocked on TASK-{prereq} landing first.", dependent),
            )
            conn.commit()
            _insert_criterion(conn, prereq, "c1")
            _insert_criterion(conn, dependent, "c1")
            _add_dep(conn, dependent, prereq, "blocks")
        finally:
            conn.close()

        rc, result, stderr = _call_start(
            db_path, config_path, str(dependent), "--force-deps"
        )

        assert rc == 0, stderr
        assert result is not None
        assert result["task"]["id"] == dependent
        assert _WARN_HEADER in stderr
        assert f"TASK-{prereq}" in stderr

    def test_unformalized_text_reference_still_warns(self, db_path, config_path):
        """A bare TASK-N text reference with no dependency row is preserved as a
        best-effort prerequisite warning (backstop for un-formalized deps)."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            other = _insert_task(conn, "loosely referenced task")
            starter = _insert_task(conn, "starting task")
            conn.execute(
                "UPDATE tasks SET description = ? WHERE id = ?",
                (f"Should probably wait for TASK-{other}.", starter),
            )
            conn.commit()
            _insert_criterion(conn, starter, "c1")
            # No task_dependencies row in either direction.
        finally:
            conn.close()

        rc, result, stderr = _call_start(db_path, config_path, str(starter))

        assert rc == 0, stderr
        assert result is not None
        assert _WARN_HEADER in stderr
        assert f"TASK-{other}" in stderr
