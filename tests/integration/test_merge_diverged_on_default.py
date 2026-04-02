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
    cherry_pick_diverged: bool = False,
    record_calls: list | None = None,
):
    """Return a mock run() for the local-merge path.

    task_on_default: when True, the branch-scoped git log --grep returns empty
    output (no exclusive [TASK-N] commits on the feature branch), simulating the
    "task commit already applied directly on default branch" scenario.
    When False, the branch-scoped log returns the feature branch's own task commit,
    meaning the task's changes are still on the feature branch and need merging.

    cherry_pick_diverged: when True, the git log --grep returns non-empty (branch has
    its own [TASK-N] commit) but git cherry returns all '-' lines (the commit was
    cherry-picked to the default branch). This simulates the cherry-pick-diverged case.
    Only meaningful when task_on_default=False.
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
        # Branch-scoped git log: git log <branch> --not <default> --grep=\[TASK-N\]
        # task_on_default=True  → empty output  (no exclusive branch commits → task already on default)
        # task_on_default=False → non-empty output (feature branch has its own task commit)
        if args[:2] == ["git", "log"] and any(f"--grep=\\[TASK-{task_id}\\]" in a for a in args):
            if task_on_default:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                args, 0,
                stdout=f"abc1234 [TASK-{task_id}] implement fix\n",
                stderr="",
            )
        # git cherry <default> <branch>: secondary cherry-pick detection
        # cherry_pick_diverged=True  → all '-' lines (every commit already on default)
        # cherry_pick_diverged=False → '+' line (commit not yet on default, normal path)
        if args[:2] == ["git", "cherry"]:
            if cherry_pick_diverged:
                return subprocess.CompletedProcess(
                    args, 0,
                    stdout=f"- abc1234abc1234abc1234abc1234abc1234abc1234\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args, 0,
                stdout=f"+ abc1234abc1234abc1234abc1234abc1234abc1234\n",
                stderr="",
            )
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
        # task_on_default=False → branch-scoped log returns non-empty → feature branch has its own commit → normal ff-merge path
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


class TestRecycledTaskId:
    """Regression: recycled task ID with a prior [TASK-N] commit on main must not skip ff-merge."""

    def _make_run_with_prior_epoch_commit(
        self,
        branch_name: str,
        task_id: int,
        default_branch: str = "main",
        record_calls: list | None = None,
    ):
        """Mock run() where main has an old [TASK-N] commit from a prior DB epoch.

        The branch-scoped log (git log <branch> --not <default> --grep) returns the
        feature branch's own task commit (non-empty), while a naïve git log on the
        default branch would also match the old epoch commit.
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
            if "pull" in args and "origin" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            # Branch-scoped log: returns feature branch's own task commit (non-empty).
            # This means task_on_default=False → ff-merge proceeds normally.
            if args[:2] == ["git", "log"] and any(f"--grep=\\[TASK-{task_id}\\]" in a for a in args):
                return subprocess.CompletedProcess(
                    args, 0,
                    stdout=f"84cfeaa [TASK-{task_id}] implement the new fix\n",
                    stderr="",
                )
            # git cherry: the new commit on the feature branch is NOT yet on default
            # (different patch content from the old epoch commit). Returns '+' line
            # so task_on_default stays False and ff-merge proceeds.
            if args[:2] == ["git", "cherry"]:
                return subprocess.CompletedProcess(
                    args, 0,
                    stdout="+ 84cfeaa84cfeaa84cfeaa84cfeaa84cfeaa84cfeaa\n",
                    stderr="",
                )
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

    def test_ff_merge_not_skipped_due_to_prior_epoch_commit(self, db_path, config_path, monkeypatch):
        """Recycled task ID: prior [TASK-N] commit on main must not cause ff-merge to be skipped."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-new-fix"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = self._make_run_with_prior_epoch_commit(
            branch, task_id=task_id, record_calls=record
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert rc == 0, f"Expected exit 0\nstderr: {stderr_buf.getvalue()}"

        ff_calls = [c for c in record if c[:3] == ["git", "merge", "--ff-only"]]
        assert ff_calls, (
            "Expected git merge --ff-only to be called — prior epoch commit on main "
            "must not trigger the skip path"
        )

        assert "Skipping ff-only merge" not in stderr_buf.getvalue(), (
            "Expected NO 'Skipping ff-only merge' note — recycled ID commit on main "
            "must not be mistaken for the current task's commit"
        )

    def test_feature_branch_commit_not_lost(self, db_path, config_path, monkeypatch):
        """Recycled task ID: the feature branch's own commit must not be lost."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-new-fix"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = self._make_run_with_prior_epoch_commit(
            branch, task_id=task_id, record_calls=record
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        # Task must be marked Done via the normal ff-merge path (not silently lost)
        task_done_calls = [c for c in record if "task-done" in c]
        assert task_done_calls, "Expected task-done to be called — commit must not be silently lost"

        result = json.loads(stdout_buf.getvalue())
        assert result["task"]["status"] == "Done"


class TestCherryPickDiverged:
    """tusk merge handles a feature branch whose commit was cherry-picked to default.

    The branch-scoped log finds the feature branch's own [TASK-N] commit (non-empty),
    but git cherry reveals it was cherry-picked — all lines are '-'. The merge should
    skip ff-only, force-delete the branch, push, close the session, and mark Done.
    """

    def test_exits_zero(self, db_path, config_path, monkeypatch):
        """main() exits 0 when feature branch commit was cherry-picked to default."""
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
        mock_run, _ = _make_run(
            branch, task_id=task_id, task_on_default=False,
            cherry_pick_diverged=True, record_calls=record
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert rc == 0, f"Expected exit 0\nstderr: {stderr_buf.getvalue()}"

    def test_ff_merge_not_called(self, db_path, config_path, monkeypatch):
        """git merge --ff-only is NOT called when commit was cherry-picked to default."""
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
        mock_run, _ = _make_run(
            branch, task_id=task_id, task_on_default=False,
            cherry_pick_diverged=True, record_calls=record
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        ff_calls = [c for c in record if c[:3] == ["git", "merge", "--ff-only"]]
        assert not ff_calls, (
            f"Expected git merge --ff-only NOT to be called when cherry-picked, got: {ff_calls}"
        )

    def test_branch_force_deleted(self, db_path, config_path, monkeypatch):
        """Diverged cherry-pick feature branch is force-deleted with -D."""
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
        mock_run, _ = _make_run(
            branch, task_id=task_id, task_on_default=False,
            cherry_pick_diverged=True, record_calls=record
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        force_delete_calls = [c for c in record if c[:3] == ["git", "branch", "-D"]]
        safe_delete_calls = [c for c in record if c[:3] == ["git", "branch", "-d"]]
        assert force_delete_calls, "Expected git branch -D to be called for cherry-pick-diverged branch"
        assert not safe_delete_calls, (
            f"Expected git branch -d NOT to be called, got: {safe_delete_calls}"
        )

    def test_prints_cherry_pick_note(self, db_path, config_path, monkeypatch):
        """Prints cherry-pick note when commit was cherry-picked to default branch."""
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
        mock_run, _ = _make_run(
            branch, task_id=task_id, task_on_default=False,
            cherry_pick_diverged=True, record_calls=record
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert "cherry-pick" in stderr_buf.getvalue(), (
            f"Expected cherry-pick note in stderr:\n{stderr_buf.getvalue()}"
        )

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
        mock_run, _ = _make_run(
            branch, task_id=task_id, task_on_default=False,
            cherry_pick_diverged=True, record_calls=record
        )
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

    def test_recycled_id_unaffected_by_cherry_check(self, db_path, config_path, monkeypatch):
        """Recycled task ID: git cherry returning '+' keeps task_on_default=False."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-new-fix"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        # cherry_pick_diverged=False → git cherry returns '+' → task_on_default stays False
        mock_run, _ = _make_run(
            branch, task_id=task_id, task_on_default=False,
            cherry_pick_diverged=False, record_calls=record
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert rc == 0, f"Expected exit 0\nstderr: {stderr_buf.getvalue()}"

        ff_calls = [c for c in record if c[:3] == ["git", "merge", "--ff-only"]]
        assert ff_calls, (
            "Expected git merge --ff-only to be called — git cherry '+' must not trigger skip path"
        )

        assert "cherry-pick" not in stderr_buf.getvalue(), (
            "Expected NO cherry-pick note when git cherry reports unapplied commits"
        )
