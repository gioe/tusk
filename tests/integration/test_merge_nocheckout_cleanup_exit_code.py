"""Regression test for TASK-504.

After a successful no-checkout fast-forward push + session-close +
task-done, ``tusk merge`` used to return exit code 0 even when
``_remove_recorded_task_worktree`` returned False (leaving the local
worktree directory and feature branch behind). Automation that checks
``$?`` could not distinguish "fully succeeded" from "succeeded but
cleanup needs manual attention" and had to grep stderr.

TASK-504 plumbs the False return value out as a distinct non-fatal
exit code (``3``). This test pins:

  1. Full success (cleanup helper returns True) → exit code 0.
  2. Partial-cleanup failure (cleanup helper returns False) → exit code 3.
  3. Cleanup-failure does NOT override a more severe ``_close_completed_task``
     failure: if close returns 2, merge still returns 2, not 3.
"""

import importlib.util
import io
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr, redirect_stdout

import pytest

from tests.integration.conftest import _insert_session, _insert_task

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


@pytest.fixture()
def nocheckout_repo(tmp_path, config_path, monkeypatch):
    """Build a tmp repo whose primary checkout has NO installed tusk binary.

    Mirrors the ``fallback_only_repo`` fixture from
    ``test_merge_worktree_cleanup_order_846.py``: the resolver falls
    through to the worktree-local fallback, which is the layout that
    drives no-checkout merges end-to-end through ``main()`` without
    needing a real tusk install on disk.
    """
    tusk_dir = tmp_path / "tusk"
    tusk_dir.mkdir(parents=True, exist_ok=True)
    db_file = tusk_dir / "tasks.db"
    monkeypatch.setenv("TUSK_DB", str(db_file))
    result = subprocess.run(
        [os.path.join(REPO_ROOT, "bin", "tusk"), "init", "--force", "--skip-gitignore"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return {"db_path": db_file, "repo_root": tmp_path}


def _stub_run_for_no_checkout_path(args, check=True):
    """Mock subprocess.run for the no-checkout merge path's git calls."""
    if args[:4] == ["git", "worktree", "list", "--porcelain"]:
        # Default-branch lock probe — report another worktree owns main.
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                "worktree /tmp/repo-main\n"
                "HEAD abc123\n"
                "branch refs/heads/main\n"
            ),
            stderr="",
        )
    if args[:3] == ["git", "remote", "get-url"]:
        return subprocess.CompletedProcess(
            args, 0, stdout="git@example.com:owner/repo.git\n", stderr=""
        )
    if args[:3] == ["git", "rev-parse", "--verify"]:
        return subprocess.CompletedProcess(args, 0, stdout="abc123\n", stderr="")
    if args[:3] == ["git", "fetch", "origin"]:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    if args[:3] == ["git", "merge-base", "--is-ancestor"]:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    if args[:2] == ["git", "log"]:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    if args[:2] == ["git", "diff"]:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    if args[:2] == ["git", "push"]:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    if args[:3] == ["git", "config", "--get"]:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
    if args[:3] == ["git", "branch", "-D"]:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    if len(args) > 0 and "session-close" in args:
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


class TestNoCheckoutCleanupExitCode:
    """Pin TASK-504's exit-code mapping for the partial-cleanup case."""

    def _run_main(self, db_path, config_path, task_id, session_id):
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [
                    str(db_path),
                    str(config_path),
                    str(task_id),
                    "--session",
                    str(session_id),
                ]
            )
        return rc, stdout_buf.getvalue(), stderr_buf.getvalue()

    def _wire_mocks(self, monkeypatch, branch, *, close_rc, cleanup_ok):
        monkeypatch.setattr(
            tusk_merge, "find_task_branch", lambda tid: (branch, None, False)
        )
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)

        def _stub_close(tusk_bin, tid, db, session_was_closed, **kwargs):
            return close_rc

        def _stub_cleanup(db, tid, br):
            return cleanup_ok

        monkeypatch.setattr(tusk_merge, "_close_completed_task", _stub_close)
        monkeypatch.setattr(
            tusk_merge, "_cleanup_no_checkout_workspace", _stub_cleanup
        )
        monkeypatch.setattr(tusk_merge, "run", _stub_run_for_no_checkout_path)

    def test_full_success_returns_zero(
        self, nocheckout_repo, config_path, monkeypatch
    ):
        """close == 0 and cleanup == True → merge exits 0."""
        db_path = nocheckout_repo["db_path"]
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()
        branch = f"feature/TASK-{task_id}-full-success"

        self._wire_mocks(monkeypatch, branch, close_rc=0, cleanup_ok=True)
        rc, _stdout, stderr = self._run_main(db_path, config_path, task_id, session_id)

        assert rc == 0, (
            f"Expected exit 0 when cleanup succeeds; got {rc}\nstderr: {stderr}"
        )

    def test_cleanup_failure_returns_three(
        self, nocheckout_repo, config_path, monkeypatch
    ):
        """close == 0 and cleanup == False → merge exits 3 (TASK-504)."""
        db_path = nocheckout_repo["db_path"]
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()
        branch = f"feature/TASK-{task_id}-cleanup-failed"

        self._wire_mocks(monkeypatch, branch, close_rc=0, cleanup_ok=False)
        rc, _stdout, stderr = self._run_main(db_path, config_path, task_id, session_id)

        assert rc == 3, (
            f"Expected exit 3 when _cleanup_no_checkout_workspace returns "
            f"False (TASK-504 partial-cleanup signal); got {rc}\n"
            f"stderr: {stderr}"
        )

    def test_close_failure_overrides_cleanup_failure(
        self, nocheckout_repo, config_path, monkeypatch
    ):
        """close == 2 and cleanup == False → merge exits 2, not 3.

        Cleanup failure is a softer signal than task-done failure;
        preserve the more severe exit code when both fire.
        """
        db_path = nocheckout_repo["db_path"]
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()
        branch = f"feature/TASK-{task_id}-close-fail"

        self._wire_mocks(monkeypatch, branch, close_rc=2, cleanup_ok=False)
        rc, _stdout, stderr = self._run_main(db_path, config_path, task_id, session_id)

        assert rc == 2, (
            f"Expected exit 2 (close failure) to override exit 3 (cleanup "
            f"failure); got {rc}\nstderr: {stderr}"
        )
