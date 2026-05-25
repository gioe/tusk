"""Regression test for issue #846.

The no-checkout fast-forward push path in ``_complete_no_checkout_fast_forward``
must call ``_close_completed_task`` (which shells out to ``tusk task-done``)
BEFORE ``_cleanup_no_checkout_workspace`` removes the task worktree. When
``_resolve_stable_tusk_bin`` falls through to the worktree-local fallback —
i.e. neither ``<primary>/.claude/bin/tusk`` nor ``<primary>/tusk/bin/tusk``
exists in the project's primary checkout — the resolved ``tusk_bin`` is the
binary inside the worktree about to be deleted. Cleanup-first ordering
deletes that binary out from under the still-pending subprocess call and
the task stays In Progress with a "Missing executable" diagnostic. This
test pins the close-then-cleanup ordering and the worktree-fallback
survival behavior. TASK-417 / issue #834 covered the primary-install
resolver case; this is the complementary fallback case.
"""

import importlib.util
import io
import json
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
def fallback_only_repo(tmp_path, config_path, monkeypatch):
    """Build a tmp repo whose primary checkout has NO installed tusk binary.

    Layout::

        tmp_path/
            tusk/tasks.db          <- TUSK_DB target (repo root for resolver)
            <no .claude/bin/tusk>
            <no tusk/bin/tusk>

    ``_resolve_stable_tusk_bin`` will fall through both candidate probes and
    return its ``fallback`` argument — which the production code derives
    from ``__file__`` (the worktree-local merge script). This is the
    layout that issue #846 exposes.
    """
    tusk_dir = tmp_path / "tusk"
    tusk_dir.mkdir(parents=True, exist_ok=True)
    db_file = tusk_dir / "tasks.db"
    monkeypatch.setenv("TUSK_DB", str(db_file))
    result = subprocess.run(
        [os.path.join(REPO_ROOT, "bin", "tusk"), "init", "--force", "--skip-gitignore"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return {"db_path": db_file, "repo_root": tmp_path}


class TestCloseTaskRunsBeforeWorktreeCleanup:
    """Pin the ordering invariant from issue #846."""

    def test_close_completed_task_called_before_cleanup(
        self, fallback_only_repo, config_path, monkeypatch
    ):
        db_path = fallback_only_repo["db_path"]

        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-cleanup-order"

        monkeypatch.setattr(
            tusk_merge, "find_task_branch", lambda tid: (branch, None, False)
        )
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)

        # Simulate the worktree-local invocation: __file__ points inside a
        # would-be task worktree. With no primary install present (see
        # fallback_only_repo), _resolve_stable_tusk_bin will return this
        # path as the fallback — exposing the issue #846 condition.
        fake_worktree_bin = (
            "/nonexistent/.tusk/worktrees/TASK-X/.claude/bin/tusk-merge.py"
        )
        monkeypatch.setattr(tusk_merge, "__file__", fake_worktree_bin)

        call_order: list[str] = []

        def _spy_close(tusk_bin, tid, db, session_was_closed, merge_commit_sha=None):
            call_order.append("close")
            return 0

        def _spy_cleanup(db, tid, br):
            call_order.append("cleanup")

        monkeypatch.setattr(tusk_merge, "_close_completed_task", _spy_close)
        monkeypatch.setattr(
            tusk_merge, "_cleanup_no_checkout_workspace", _spy_cleanup
        )

        def _mock_run(args, check=True):
            # Default-branch lock probe — report another worktree owns main.
            if args[:4] == ["git", "worktree", "list", "--porcelain"]:
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
                return subprocess.CompletedProcess(
                    args, 0, stdout="abc123\n", stderr=""
                )
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
            # Any tusk subprocess (e.g. session-close) — return success.
            if len(args) > 0 and "session-close" in args:
                return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _mock_run)

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

        assert rc == 0, (
            f"Expected exit 0\nstderr: {stderr_buf.getvalue()}\norder: {call_order}"
        )
        assert call_order == ["close", "cleanup"], (
            "Issue #846: _close_completed_task must run BEFORE "
            f"_cleanup_no_checkout_workspace; got: {call_order}"
        )

    def test_main_returns_close_rc_when_cleanup_follows(
        self, fallback_only_repo, config_path, monkeypatch
    ):
        """main() returns the rc from _close_completed_task — cleanup is
        deferred but cannot mask a task-done failure."""
        db_path = fallback_only_repo["db_path"]

        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-close-rc"

        monkeypatch.setattr(
            tusk_merge, "find_task_branch", lambda tid: (branch, None, False)
        )
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)

        cleanup_called = []

        def _failing_close(tusk_bin, tid, db, session_was_closed, merge_commit_sha=None):
            return 2

        def _spy_cleanup(db, tid, br):
            cleanup_called.append(True)

        monkeypatch.setattr(tusk_merge, "_close_completed_task", _failing_close)
        monkeypatch.setattr(
            tusk_merge, "_cleanup_no_checkout_workspace", _spy_cleanup
        )

        def _mock_run(args, check=True):
            if args[:4] == ["git", "worktree", "list", "--porcelain"]:
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
                return subprocess.CompletedProcess(
                    args, 0, stdout="abc123\n", stderr=""
                )
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

        monkeypatch.setattr(tusk_merge, "run", _mock_run)

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

        assert rc == 2, (
            f"Expected rc=2 propagated from _close_completed_task; got {rc}\n"
            f"stderr: {stderr_buf.getvalue()}"
        )
        assert cleanup_called == [True], (
            "Cleanup must still run even when task-done returned non-zero — "
            "the recorded worktree row is otherwise leaked. "
            f"got cleanup_called={cleanup_called}"
        )


class TestFallbackBinarySurvivesUntilCloseTask:
    """When _resolve_stable_tusk_bin returns the worktree-local fallback
    (the issue #846 condition), the binary must remain on disk through
    the close-task subprocess call. The fix achieves this structurally by
    moving cleanup after close-task rather than by changing the resolver.
    """

    def test_fallback_returned_when_primary_install_absent(self, tmp_path):
        """Sanity-pin the resolver behavior the ordering fix relies on:
        with no primary install present, the resolver returns its
        ``fallback`` argument unchanged — i.e. the worktree-local path
        derived from ``__file__``.
        """
        (tmp_path / "tusk").mkdir(parents=True)
        db = tmp_path / "tusk" / "tasks.db"
        db.write_text("")
        fake_worktree_bin = (
            "/nonexistent/.tusk/worktrees/TASK-X/.claude/bin/tusk"
        )

        assert (
            tusk_merge._resolve_stable_tusk_bin(str(db), fake_worktree_bin)
            == fake_worktree_bin
        )
