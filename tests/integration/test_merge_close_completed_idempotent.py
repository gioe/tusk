"""Regression coverage for issue #943 — _close_completed_task idempotent retry.

When a prior `tusk merge` invocation marked the task Done but failed during
worktree cleanup (e.g. untracked symlinks blocked `git worktree remove`),
rerunning `tusk merge` must finish the cleanup the prior attempt left behind
instead of failing with exit 2 because task-done reports "is already Done".

The fix lives in `_close_completed_task` (bin/tusk-merge.py): when the
`task-done` subprocess exits 2 with stderr matching `is already Done`
(case-insensitive), the function re-fetches the task row from the live DB and,
when status='Done' AND closed_reason='completed', returns 0 with a synthetic
JSON payload. Tasks Done with any other closed_reason (wont_do/expired/duplicate)
fall through to the existing exit-2 path so genuine state mismatches still
surface loudly.
"""

import importlib.util
import io
import json
import os
import subprocess
from contextlib import redirect_stderr, redirect_stdout

import pytest

from tests.integration.conftest import _insert_task, _insert_session

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        os.path.join(REPO_ROOT, "bin", f"{name}.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_merge = _load("tusk-merge")


def _mark_task_done(db_path, task_id: int, closed_reason: str) -> None:
    """Flip a task row to Done with a specific closed_reason.

    Simulates the state left behind by a prior `tusk merge` invocation that
    completed task-done but failed during worktree cleanup.
    """
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE tasks SET status='Done', closed_reason=?, closed_at=datetime('now') "
            "WHERE id=?",
            (closed_reason, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _stub_already_done_run(task_id: int):
    """Return a _run_tusk_subcommand stub that mimics task-done refusing because
    the row is already Done — the failure mode the rerun path actually hits."""
    def _run(tusk_bin, args, **kwargs):
        return subprocess.CompletedProcess(
            [tusk_bin, *args],
            returncode=2,
            stdout="",
            stderr=f"Error: Task {task_id} is already Done\n",
        )
    return _run


class TestCloseCompletedTaskIdempotent:
    """Cover the idempotent retry branch in _close_completed_task (issue #943)."""

    def test_returns_zero_when_task_already_done_completed(
        self, db_path, monkeypatch
    ):
        """Rerun path: task is already Done with closed_reason='completed' → return 0."""
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
        finally:
            conn.close()
        _mark_task_done(db_path, task_id, "completed")

        monkeypatch.setattr(
            tusk_merge, "_run_tusk_subcommand", _stub_already_done_run(task_id)
        )
        # Stamping the merge SHAs is a side-channel best-effort write — make it
        # a no-op so we isolate the idempotent branch under test.
        monkeypatch.setattr(
            tusk_merge, "_stamp_merge_commit_sha", lambda *a, **k: None
        )

        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = tusk_merge._close_completed_task(
                "tusk", task_id, str(db_path), session_was_closed=True,
            )

        assert rc == 0, (
            f"Expected idempotent retry to return 0; got {rc}\n"
            f"stderr: {err_buf.getvalue()}"
        )
        # Diagnostic note matches the sibling session-close "already closed" branch
        # at bin/tusk-merge.py:1628-1632 in tone — names the task and the action.
        stderr = err_buf.getvalue()
        assert "was already closed by a prior merge attempt" in stderr, (
            f"Expected idempotent-retry warning in stderr; got:\n{stderr}"
        )
        # Synthetic JSON mirrors the WAL-recovery shape (task, sessions_closed,
        # unblocked_tasks) plus an idempotent_retry marker so downstream
        # tooling can distinguish this path from a fresh close.
        payload = json.loads(out_buf.getvalue())
        assert payload["task"]["id"] == task_id
        assert payload["task"]["status"] == "Done"
        assert payload["task"]["closed_reason"] == "completed"
        assert payload["sessions_closed"] == 1
        assert payload["unblocked_tasks"] == []
        assert payload.get("idempotent_retry") is True

    @pytest.mark.parametrize("closed_reason", ["wont_do", "duplicate", "expired"])
    def test_returns_two_when_closed_reason_not_completed(
        self, db_path, monkeypatch, closed_reason
    ):
        """Negative case: task is Done with a non-completed closed_reason →
        idempotent branch refuses and the existing exit-2 path fires unchanged.

        This guards the safety net for genuine state mismatches: a task that was
        explicitly closed as wont_do/duplicate/expired must NOT be silently
        treated as a successful merge just because task-done refuses with
        'is already Done'.
        """
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
        finally:
            conn.close()
        _mark_task_done(db_path, task_id, closed_reason)

        monkeypatch.setattr(
            tusk_merge, "_run_tusk_subcommand", _stub_already_done_run(task_id)
        )
        monkeypatch.setattr(
            tusk_merge, "_stamp_merge_commit_sha", lambda *a, **k: None
        )

        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = tusk_merge._close_completed_task(
                "tusk", task_id, str(db_path), session_was_closed=True,
            )

        assert rc == 2, (
            f"Expected exit 2 for closed_reason={closed_reason!r}; got {rc}\n"
            f"stderr: {err_buf.getvalue()}"
        )
        stderr = err_buf.getvalue()
        assert "task-done failed" in stderr, (
            f"Expected existing exit-2 diagnostic; got:\n{stderr}"
        )
        # The idempotent warning must NOT have fired.
        assert "already closed by a prior merge attempt" not in stderr

    def test_returns_two_when_stderr_lies_about_already_done(
        self, db_path, monkeypatch
    ):
        """Defensive: task-done says 'is already Done' but the row is not
        actually Done (e.g. a race where the row was rolled back, or a
        malformed error message). The DB-confirmation guard must reject
        the idempotent branch and surface the original exit 2 unchanged.
        """
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)  # leaves status='In Progress'
        finally:
            conn.close()

        monkeypatch.setattr(
            tusk_merge, "_run_tusk_subcommand", _stub_already_done_run(task_id)
        )
        monkeypatch.setattr(
            tusk_merge, "_stamp_merge_commit_sha", lambda *a, **k: None
        )

        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            rc = tusk_merge._close_completed_task(
                "tusk", task_id, str(db_path), session_was_closed=True,
            )

        assert rc == 2, (
            f"Expected exit 2 when row is not actually Done; got {rc}\n"
            f"stderr: {err_buf.getvalue()}"
        )
        assert "already closed by a prior merge attempt" not in err_buf.getvalue()

    def test_sessions_closed_reflects_caller_flag(self, db_path, monkeypatch):
        """sessions_closed in the synthetic payload echoes the session_was_closed
        argument — same accounting the WAL-recovery branch already does so
        downstream JSON consumers see consistent counts on both recovery paths.
        """
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
        finally:
            conn.close()
        _mark_task_done(db_path, task_id, "completed")

        monkeypatch.setattr(
            tusk_merge, "_run_tusk_subcommand", _stub_already_done_run(task_id)
        )
        monkeypatch.setattr(
            tusk_merge, "_stamp_merge_commit_sha", lambda *a, **k: None
        )

        out_buf = io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(io.StringIO()):
            tusk_merge._close_completed_task(
                "tusk", task_id, str(db_path), session_was_closed=False,
            )

        payload = json.loads(out_buf.getvalue())
        assert payload["sessions_closed"] == 0
