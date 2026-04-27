"""Tests for the unpushed-local-default-commits guard in tusk merge (issue #607).

Covers:
- Guard fires on the rebase path when local <default> is ahead of origin/<default>
- Guard fires on the ff-only path (no --rebase) for the same condition
- In a non-interactive context (no TTY on stdin) the merge aborts with exit 2
- Surfaced commits include the diverging SHA + subject lines
- Push is NOT attempted after the abort
- When local default == origin/default the guard is silent and the merge proceeds
- When the origin ref is missing (never-fetched repo) the guard is silent
"""

import importlib.util
import io
import os
import sqlite3
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


def _mock_run_factory(
    *,
    branch_name: str,
    default_branch: str = "main",
    task_id: int,
    unpushed_commits: list[tuple[str, str]] | None,
    use_rebase_succeeds: bool = True,
    ff_only_succeeds: bool = True,
    record_calls: list | None = None,
):
    """Build a mock run() with knobs for the unpushed-default scenarios.

    unpushed_commits=None → origin/<default> ref is reported missing (rev-parse fails)
    unpushed_commits=[]   → ref exists, no commits ahead (clean state)
    unpushed_commits=[(sha, subject), ...] → ref exists and N commits are ahead
    """
    calls = record_calls if record_calls is not None else []

    def _run(args, check=True):
        calls.append(list(args))

        # Diff/stash bookkeeping — always clean
        if args[:2] == ["git", "diff"] and "--name-only" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "stash", "push"]:
            return subprocess.CompletedProcess(
                args, 0, stdout="No local changes to save", stderr=""
            )
        if args[:2] == ["git", "checkout"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:1] == ["git"] and "pull" in args and "origin" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(
                args, 0, stdout="git@example.com:owner/repo.git\n", stderr=""
            )

        # The unpushed-commits guard issues these two calls in order:
        #   1. git rev-parse --verify refs/remotes/origin/<default>
        #   2. git log --format=%h %s refs/remotes/origin/<default>..<default>
        if (
            args[:3] == ["git", "rev-parse", "--verify"]
            and len(args) == 4
            and args[3] == f"refs/remotes/origin/{default_branch}"
        ):
            if unpushed_commits is None:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="fatal: bad ref")
            return subprocess.CompletedProcess(
                args, 0, stdout="0123456789abcdef\n", stderr=""
            )
        if (
            args[:2] == ["git", "log"]
            and "--format=%h %s" in args
            and any(a == f"refs/remotes/origin/{default_branch}..{default_branch}" for a in args)
        ):
            if not unpushed_commits:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            stdout = "".join(f"{sha} {subject}\n" for sha, subject in unpushed_commits)
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        # Branch-scoped grep (existing pattern from test_merge_rebase_flag.py)
        if args[:2] == ["git", "log"] and any(
            f"--grep=\\[TASK-{task_id}\\]" in a for a in args
        ):
            return subprocess.CompletedProcess(
                args, 0, stdout=f"abc1234 [TASK-{task_id}] implement\n", stderr=""
            )

        # Cherry detection — say nothing was cherry-picked
        if args[:2] == ["git", "cherry"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        # rev-list --count for "no_new_commits" detection
        if args[:3] == ["git", "rev-list", "--count"]:
            return subprocess.CompletedProcess(args, 0, stdout="1\n", stderr="")

        if args[:3] == ["git", "rebase", default_branch]:
            return subprocess.CompletedProcess(
                args,
                0 if use_rebase_succeeds else 1,
                stdout="",
                stderr="" if use_rebase_succeeds else "CONFLICT (content)",
            )
        if args[:3] == ["git", "merge", "--ff-only"]:
            return subprocess.CompletedProcess(
                args,
                0 if ff_only_succeeds else 1,
                stdout="",
                stderr="" if ff_only_succeeds else "fatal: not fast-forward",
            )
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "branch", "-d"] or args[:3] == ["git", "branch", "-D"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if "session-close" in args:
            return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")
        if "task-done" in args:
            import json as _json
            payload = _json.dumps({
                "task": {"id": task_id, "status": "Done", "closed_reason": "completed"},
                "sessions_closed": 0,
                "unblocked_tasks": [],
            })
            return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")

        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return _run, calls


def _setup_task_session(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        task_id = _insert_task(conn)
        session_id = _insert_session(conn, task_id)
    finally:
        conn.close()
    return task_id, session_id


class TestRebasePathGuard:
    """When local default is ahead of origin/default, the rebase path aborts before pushing."""

    def test_rebase_aborts_when_unpushed_commits_present(self, db_path, config_path, monkeypatch):
        task_id, session_id = _setup_task_session(db_path)
        branch = f"feature/TASK-{task_id}-x"
        unpushed = [
            ("f3c62520", "[TASK-1733] Apply review fixes (post-merge)"),
            ("aabbccdd", "[TASK-9999] Unrelated tweak"),
        ]
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _mock_run_factory(
            branch_name=branch, task_id=task_id,
            unpushed_commits=unpushed, record_calls=record,
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main([
                str(db_path), str(config_path), str(task_id),
                "--session", str(session_id), "--rebase",
            ])

        assert rc == 2, f"Expected exit 2, got {rc}\nstderr: {stderr_buf.getvalue()}"
        stderr = stderr_buf.getvalue()
        assert "ahead of 'origin/main'" in stderr
        assert "f3c62520" in stderr
        assert "[TASK-1733]" in stderr
        assert "aabbccdd" in stderr
        assert "[TASK-9999]" in stderr

        # Push must not have been attempted
        push_calls = [c for c in record if c[:3] == ["git", "push", "origin"]]
        assert not push_calls, f"Expected no push, got: {push_calls}"

        # Rebase must not have been attempted (guard runs before the rebase block)
        rebase_calls = [
            c for c in record
            if c[:2] == ["git", "rebase"] and c[2:3] != ["--abort"]
        ]
        assert not rebase_calls, f"Expected no rebase, got: {rebase_calls}"


class TestFfOnlyPathGuard:
    """The non-rebase ff-only path runs the same check before pushing."""

    def test_ff_only_aborts_when_unpushed_commits_present(self, db_path, config_path, monkeypatch):
        task_id, session_id = _setup_task_session(db_path)
        branch = f"feature/TASK-{task_id}-x"
        unpushed = [("deadbeef", "[TASK-2000] Stale local commit")]
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _mock_run_factory(
            branch_name=branch, task_id=task_id,
            unpushed_commits=unpushed, record_calls=record,
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main([
                str(db_path), str(config_path), str(task_id),
                "--session", str(session_id),
            ])

        assert rc == 2, f"Expected exit 2, got {rc}\nstderr: {stderr_buf.getvalue()}"
        stderr = stderr_buf.getvalue()
        assert "deadbeef" in stderr
        assert "[TASK-2000]" in stderr

        push_calls = [c for c in record if c[:3] == ["git", "push", "origin"]]
        assert not push_calls
        ff_calls = [c for c in record if c[:3] == ["git", "merge", "--ff-only"]]
        assert not ff_calls, "ff-only merge must not run after the guard aborts"


class TestNonInteractiveAbort:
    """Without a TTY on stdin (default test environment), the guard never proceeds."""

    def test_aborts_with_clear_error_message(self, db_path, config_path, monkeypatch):
        task_id, session_id = _setup_task_session(db_path)
        branch = f"feature/TASK-{task_id}-x"
        unpushed = [("deadbeef", "[TASK-2000] Unrelated")]

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _mock_run_factory(
            branch_name=branch, task_id=task_id, unpushed_commits=unpushed,
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main([
                str(db_path), str(config_path), str(task_id),
                "--session", str(session_id), "--rebase",
            ])

        assert rc == 2
        stderr = stderr_buf.getvalue()
        # Non-interactive abort message must surface a remediation path
        assert "git push origin main" in stderr
        assert "git fetch origin" in stderr or "git reset --hard origin/main" in stderr


class TestGuardSilentWhenClean:
    """When local default == origin/default, the guard is invisible and the merge proceeds."""

    def test_no_unpushed_commits_proceeds_to_merge(self, db_path, config_path, monkeypatch):
        task_id, session_id = _setup_task_session(db_path)
        branch = f"feature/TASK-{task_id}-x"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _mock_run_factory(
            branch_name=branch, task_id=task_id,
            unpushed_commits=[], record_calls=record,
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main([
                str(db_path), str(config_path), str(task_id),
                "--session", str(session_id), "--rebase",
            ])

        assert rc == 0, f"Expected success, got rc={rc}\nstderr: {stderr_buf.getvalue()}"
        # Merge must have proceeded all the way to ff-only and push
        ff_calls = [c for c in record if c[:3] == ["git", "merge", "--ff-only"]]
        push_calls = [c for c in record if c[:3] == ["git", "push", "origin"]]
        assert ff_calls and push_calls

    def test_missing_origin_ref_is_silent(self, db_path, config_path, monkeypatch):
        """Never-fetched repos report origin/<default> missing — guard skips silently."""
        task_id, session_id = _setup_task_session(db_path)
        branch = f"feature/TASK-{task_id}-x"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _mock_run_factory(
            branch_name=branch, task_id=task_id,
            unpushed_commits=None, record_calls=record,
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main([
                str(db_path), str(config_path), str(task_id),
                "--session", str(session_id),
            ])

        assert rc == 0
        assert "ahead of 'origin/main'" not in stderr_buf.getvalue()
