"""Integration tests for the unmet-`blocks`-dependency guard in task-start.

Issue #626: `tusk task-start <id>` previously had no dep check at all — it only
guarded against zero criteria and unresolved external blockers, which meant
`--force` (documented as bypassing only the zero-criteria guard) silently
appeared to bypass dep blocking too. The fix adds a `blocks`-type dep guard
mirroring `v_ready_tasks` semantics, with a new `--force-deps` flag for the
explicit bypass case. Contingent deps remain non-blocking per docs/GLOSSARY.md.
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


def _insert_task(
    conn: sqlite3.Connection,
    summary: str,
    *,
    status: str = "To Do",
) -> int:
    cur = conn.execute(
        "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score)"
        " VALUES (?, ?, 'feature', 'Medium', 'S', 50)",
        (summary, status),
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


class TestBlocksDepGuard:
    def test_blocks_dep_refuses_start(self, db_path, config_path):
        """task-start refuses when an unmet `blocks` dep exists."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            blocker = _insert_task(conn, "blocker task")
            _insert_criterion(conn, blocker, "c1")
            blocked = _insert_task(conn, "blocked task")
            _insert_criterion(conn, blocked, "c1")
            _add_dep(conn, blocked, blocker, "blocks")
        finally:
            conn.close()

        rc, result, stderr = _call_start(db_path, config_path, str(blocked))

        assert rc == 2
        assert result is None
        assert "blocked by unmet 'blocks' dependencies" in stderr
        assert f"TASK-{blocker}" in stderr
        assert "--force-deps" in stderr

    def test_force_does_not_bypass_dep_guard(self, db_path, config_path):
        """The original --force flag (criteria bypass) must NOT bypass dep blocking.

        This is the regression directly from issue #626: the reporter ran
        `tusk task-start $BLOCKED --force` and the call succeeded. Post-fix it
        must error and surface --force-deps as the explicit bypass.
        """
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            blocker = _insert_task(conn, "blocker task")
            _insert_criterion(conn, blocker, "c1")
            blocked = _insert_task(conn, "blocked task")
            _insert_criterion(conn, blocked, "c1")
            _add_dep(conn, blocked, blocker, "blocks")
        finally:
            conn.close()

        rc, result, stderr = _call_start(db_path, config_path, str(blocked), "--force")

        assert rc == 2
        assert result is None
        assert "blocked by unmet 'blocks' dependencies" in stderr
        assert "--force-deps" in stderr

    def test_force_deps_bypasses_with_warning(self, db_path, config_path):
        """--force-deps lets the start proceed but emits a warning naming the blocker."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            blocker = _insert_task(conn, "blocker task")
            _insert_criterion(conn, blocker, "c1")
            blocked = _insert_task(conn, "blocked task")
            _insert_criterion(conn, blocked, "c1")
            _add_dep(conn, blocked, blocker, "blocks")
        finally:
            conn.close()

        rc, result, stderr = _call_start(
            db_path, config_path, str(blocked), "--force-deps"
        )

        assert rc == 0, stderr
        assert result is not None
        assert result["task"]["id"] == blocked
        assert result["task"]["status"] == "In Progress"
        assert "Proceeding anyway due to --force-deps" in stderr
        assert f"TASK-{blocker}" in stderr

    def test_resolved_blocker_does_not_block(self, db_path, config_path):
        """When the upstream blocker is Done, the dep guard does not fire."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            blocker = _insert_task(conn, "done blocker", status="Done")
            blocked = _insert_task(conn, "ready dependent")
            _insert_criterion(conn, blocked, "c1")
            _add_dep(conn, blocked, blocker, "blocks")
        finally:
            conn.close()

        rc, result, stderr = _call_start(db_path, config_path, str(blocked))

        assert rc == 0, stderr
        assert result is not None
        assert result["task"]["id"] == blocked
        assert result["task"]["status"] == "In Progress"

    def test_contingent_dep_does_not_block(self, db_path, config_path):
        """Contingent deps are documented (docs/GLOSSARY.md) to NOT block start.

        Locks the chosen scope: this fix only enforces `blocks`-type predicate
        (matching v_ready_tasks). Changing contingent semantics is a separate
        design question.
        """
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            upstream = _insert_task(conn, "contingent upstream")
            _insert_criterion(conn, upstream, "c1")
            downstream = _insert_task(conn, "contingent downstream")
            _insert_criterion(conn, downstream, "c1")
            _add_dep(conn, downstream, upstream, "contingent")
        finally:
            conn.close()

        rc, result, stderr = _call_start(db_path, config_path, str(downstream))

        assert rc == 0, stderr
        assert result is not None
        assert result["task"]["id"] == downstream

    def test_unblocked_task_starts_cleanly(self, db_path, config_path):
        """A task with no deps and met criteria starts without warnings."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            tid = _insert_task(conn, "free task")
            _insert_criterion(conn, tid, "c1")
        finally:
            conn.close()

        rc, result, stderr = _call_start(db_path, config_path, str(tid))

        assert rc == 0, stderr
        assert result is not None
        assert result["task"]["id"] == tid
        assert result["task"]["status"] == "In Progress"
        assert "blocks" not in stderr.lower()
