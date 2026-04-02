"""Regression tests for tusk merge: diverged feature branch when task commit is
already on the default branch (issue #426).

When a fix is committed directly on the default branch (e.g. after a rebase
conflict resolved by re-applying on main), the feature branch is diverged and
git merge --ff-only would fail.  tusk merge should detect the [TASK-<id>]
commit in the default branch log, skip the ff-only merge, delete the diverged
branch with -D, push, close the session, and mark the task Done.
"""

import importlib.util
import io
import json
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


def _make_run(
    branch_name: str,
    default_branch: str = "main",
    task_id: int = 1,
    task_on_default: bool = False,
    record_calls: list | None = None,
):
    """Return a mock run() for the local-merge path.

    task_on_default: when True, git log --grep returns the task commit,
    simulating the "committed directly on default" scenario.
    """
    calls = record_calls if record_calls is not None else []

    def _run(args, check=True):
        calls.append(list(args))

        if args[:2] == ["git", "diff"] and "--name-only" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "stash", "push"]:
            return subprocess.CompletedProcess(args, 0, stdout="No local changes to save", stderr="")
        if args[:2] == ["git", "checkout"] and len(args) == 3:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        # git pull (called as ["git", "-c", "pull.rebase=false", "pull", ...])
        if "pull" in args and "origin" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        # git log --grep=\[TASK-N\] check (brackets escaped to avoid regex char-class)
        if args[:2] == ["git", "log"] and any(f"--grep=\\[TASK-{task_id}\\]" in a for a in args):
            if task_on_default:
                return subprocess.CompletedProcess(
                    args, 0,
                    stdout=f"abc1234 [TASK-{task_id}] fix applied directly on main\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "merge", "--ff-only"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] in (["git", "branch", "-d"], ["git", "branch", "-D"]):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if "session-close" in args:
            return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")
        if "task-done" in args:
            result_json = json.dumps({
                "task": {"id": task_id, "status": "Done", "closed_reason": "completed"},
                "sessions_closed": 0,
                "unblocked_tasks": [],
            })
            return subprocess.CompletedProcess(args, 0, stdout=result_json, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return _run, calls


class TestDivergedOnDefault:
    """tusk merge skips ff-only merge when task commit is already on default branch."""

    def test_exits_zero(self, db_path, config_path, monkeypatch):
        """main() exits 0 when task commit is already on the default branch."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-my-fix"
        default = "main"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: default)
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _make_run(branch, default_branch=default, task_id=task_id,
                                task_on_default=True, record_calls=record)
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert rc == 0, f"Expected exit 0\nstderr: {stderr_buf.getvalue()}"

    def test_prints_skipping_note(self, db_path, config_path, monkeypatch):
        """Prints 'Skipping ff-only merge' note when task commit is on default branch."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-my-fix"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _make_run(branch, task_id=task_id, task_on_default=True, record_calls=record)
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert "Skipping ff-only merge" in stderr_buf.getvalue(), (
            f"Expected 'Skipping ff-only merge' note in stderr:\n{stderr_buf.getvalue()}"
        )

    def test_ff_merge_not_called(self, db_path, config_path, monkeypatch):
        """git merge --ff-only is NOT called when task commit is already on default branch."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-my-fix"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _make_run(branch, task_id=task_id, task_on_default=True, record_calls=record)
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        ff_calls = [c for c in record if c[:3] == ["git", "merge", "--ff-only"]]
        assert not ff_calls, f"Expected git merge --ff-only NOT to be called, but got: {ff_calls}"

    def test_branch_force_deleted(self, db_path, config_path, monkeypatch):
        """Diverged feature branch is force-deleted with -D (not -d)."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-my-fix"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _make_run(branch, task_id=task_id, task_on_default=True, record_calls=record)
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        force_delete_calls = [c for c in record if c[:3] == ["git", "branch", "-D"]]
        safe_delete_calls = [c for c in record if c[:3] == ["git", "branch", "-d"]]
        assert force_delete_calls, "Expected git branch -D to be called for diverged branch"
        assert not safe_delete_calls, (
            f"Expected git branch -d NOT to be called, but got: {safe_delete_calls}"
        )

    def test_push_called(self, db_path, config_path, monkeypatch):
        """git push is called to publish the already-on-default commit."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-my-fix"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _make_run(branch, task_id=task_id, task_on_default=True, record_calls=record)
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        push_calls = [c for c in record if c[:2] == ["git", "push"]]
        assert push_calls, "Expected git push to be called"

    def test_task_marked_done(self, db_path, config_path, monkeypatch):
        """task-done is called and the JSON output reflects Done status."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-my-fix"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _make_run(branch, task_id=task_id, task_on_default=True, record_calls=record)
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        task_done_calls = [c for c in record if "task-done" in c]
        assert task_done_calls, "Expected task-done to be called"

        result = json.loads(stdout_buf.getvalue())
        assert result["task"]["status"] == "Done"


class TestNormalPathUnaffected:
    """Normal ff-only merge path is unaffected when task commit is NOT on default branch."""

    def test_ff_merge_called_when_not_on_default(self, db_path, config_path, monkeypatch):
        """git merge --ff-only IS called when git log finds no [TASK-N] commit on default."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-my-fix"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        # task_on_default=False → git log returns empty output → normal ff-merge path
        mock_run, _ = _make_run(branch, task_id=task_id, task_on_default=False, record_calls=record)
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert rc == 0, f"Expected exit 0\nstderr: {stderr_buf.getvalue()}"

        ff_calls = [c for c in record if c[:3] == ["git", "merge", "--ff-only"]]
        assert ff_calls, "Expected git merge --ff-only to be called on normal path"

        assert "Skipping ff-only merge" not in stderr_buf.getvalue(), (
            "Expected NO skip note on normal path"
        )

    def test_branch_safe_deleted_on_normal_path(self, db_path, config_path, monkeypatch):
        """git branch -d (not -D) is used on the normal fast-forward merge path."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-my-fix"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _make_run(branch, task_id=task_id, task_on_default=False, record_calls=record)
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        safe_delete_calls = [c for c in record if c[:3] == ["git", "branch", "-d"]]
        force_delete_calls = [c for c in record if c[:3] == ["git", "branch", "-D"]]
        assert safe_delete_calls, "Expected git branch -d on normal merge path"
        assert not force_delete_calls, (
            f"Expected git branch -D NOT to be called on normal path, got: {force_delete_calls}"
        )
