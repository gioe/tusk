"""Integration tests for task-start active-session ownership guard."""

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


def _insert_task(conn: sqlite3.Connection, summary: str) -> int:
    cur = conn.execute(
        "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score)"
        " VALUES (?, 'To Do', 'feature', 'Medium', 'S', 50)",
        (summary,),
    )
    conn.commit()
    return cur.lastrowid


def _insert_criterion(conn: sqlite3.Connection, task_id: int) -> None:
    conn.execute(
        "INSERT INTO acceptance_criteria (task_id, criterion, source, is_completed)"
        " VALUES (?, 'criterion', 'original', 0)",
        (task_id,),
    )
    conn.commit()


def _insert_workspace(
    conn: sqlite3.Connection, task_id: int, branch: str, workspace_path: str
) -> None:
    conn.execute(
        "INSERT INTO task_workspaces (task_id, branch, workspace_path)"
        " VALUES (?, ?, ?)",
        (task_id, branch, workspace_path),
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


class TestTaskStartConcurrencyGuard:
    def test_second_start_without_recorded_workspace_refuses_active_session(
        self, db_path, config_path, tmp_path, monkeypatch
    ):
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = _insert_task(conn, "no workspace session guard task")
            _insert_criterion(conn, task_id)
        finally:
            conn.close()

        rc, result, stderr = _call_start(db_path, config_path, str(task_id))
        assert rc == 0, stderr
        assert result is not None

        checkout = tmp_path / "checkout"
        checkout.mkdir()
        monkeypatch.setenv("TUSK_REPO_ROOT", str(checkout))

        rc, result, stderr = _call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 2
        assert result is None
        assert "already has an active session" in stderr
        assert "No recorded task workspace was found" in stderr
        assert "--force-session" in stderr

    def test_second_start_outside_task_workspace_refuses_active_session(
        self, db_path, config_path, tmp_path, monkeypatch
    ):
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = _insert_task(conn, "session guard task")
            _insert_criterion(conn, task_id)
            workspace = tmp_path / "TASK-session-guard"
            workspace.mkdir()
            _insert_workspace(
                conn,
                task_id,
                "feature/TASK-session-guard",
                str(workspace),
            )
        finally:
            conn.close()

        rc, result, stderr = _call_start(db_path, config_path, str(task_id))
        assert rc == 0, stderr
        assert result is not None

        other_checkout = tmp_path / "other-checkout"
        other_checkout.mkdir()
        monkeypatch.setenv("TUSK_REPO_ROOT", str(other_checkout))

        rc, result, stderr = _call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 2
        assert result is None
        assert "already has an active session" in stderr
        assert str(workspace) in stderr
        assert str(other_checkout) in stderr
        assert "--force-session" in stderr

    def test_start_from_recorded_task_workspace_reuses_active_session(
        self, db_path, config_path, tmp_path, monkeypatch
    ):
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = _insert_task(conn, "same workspace task")
            _insert_criterion(conn, task_id)
            workspace = tmp_path / "TASK-same-workspace"
            workspace.mkdir()
            _insert_workspace(
                conn,
                task_id,
                "feature/TASK-same-workspace",
                str(workspace),
            )
        finally:
            conn.close()

        rc, result, stderr = _call_start(db_path, config_path, str(task_id))
        assert rc == 0, stderr
        assert result is not None
        session_id = result["session_id"]

        monkeypatch.setenv("TUSK_REPO_ROOT", str(workspace))

        rc, result, stderr = _call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 0, stderr
        assert result is not None
        assert result["session_id"] == session_id
        assert "already has an active session" not in stderr

    def test_force_session_explicitly_reuses_active_session_outside_workspace(
        self, db_path, config_path, tmp_path, monkeypatch
    ):
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = _insert_task(conn, "forced session reuse task")
            _insert_criterion(conn, task_id)
            workspace = tmp_path / "TASK-forced-session"
            workspace.mkdir()
            _insert_workspace(
                conn,
                task_id,
                "feature/TASK-forced-session",
                str(workspace),
            )
        finally:
            conn.close()

        rc, result, stderr = _call_start(db_path, config_path, str(task_id))
        assert rc == 0, stderr
        assert result is not None
        session_id = result["session_id"]

        other_checkout = tmp_path / "other-checkout"
        other_checkout.mkdir()
        monkeypatch.setenv("TUSK_REPO_ROOT", str(other_checkout))

        rc, result, stderr = _call_start(
            db_path, config_path, str(task_id), "--force-session"
        )

        assert rc == 0, stderr
        assert result is not None
        assert result["session_id"] == session_id
        assert "already has an active session" in stderr
        assert "--force-session" in stderr
