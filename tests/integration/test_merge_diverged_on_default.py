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


def _patch_overlap_helpers(
    monkeypatch,
    matched_default_shas: list[str] | None = None,
    matched_files: set[str] | None = None,
):
    """Monkeypatch the file-overlap helpers introduced for issue #656.

    `find_task_commits` and `commit_changed_files` live in tusk-git-helpers.py and
    use `subprocess.run` directly (not `tusk_merge.run`), so the existing run-mock
    can't intercept them. Patch them on the tusk_merge module namespace so the
    override path in tusk-merge.py sees deterministic values.

    matched_default_shas: SHAs returned by find_task_commits(<task_id>, [default]).
    Defaults to a non-empty sentinel so the prefix-collision file-overlap heuristic
    sees a matched commit on default and keeps task_on_default=True for tests whose
    inserted task has no scope signal. Pass [] to model the issue #656 incident
    (no [TASK-N] commits on default at all).

    matched_files: file paths returned by commit_changed_files for those SHAs.
    """
    if matched_default_shas is None:
        matched_default_shas = ["deadbeef" + "0" * 32]
    if matched_files is None:
        matched_files = {"some/file.py"}
    monkeypatch.setattr(
        tusk_merge,
        "find_task_commits",
        lambda task_id, repo_root, refs=None, since=None: list(matched_default_shas),
    )
    monkeypatch.setattr(
        tusk_merge,
        "commit_changed_files",
        lambda commits, repo_root: set(matched_files),
    )


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
                    stdout="- abc1234abc1234abc1234abc1234abc1234abc1234\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args, 0,
                stdout="+ abc1234abc1234abc1234abc1234abc1234abc1234\n",
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
        _patch_overlap_helpers(monkeypatch)
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
        _patch_overlap_helpers(monkeypatch)
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
        _patch_overlap_helpers(monkeypatch)
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
        _patch_overlap_helpers(monkeypatch)
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
        _patch_overlap_helpers(monkeypatch)
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
        _patch_overlap_helpers(monkeypatch)
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
        _patch_overlap_helpers(monkeypatch)
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
        _patch_overlap_helpers(monkeypatch)
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
        _patch_overlap_helpers(monkeypatch)
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
        _patch_overlap_helpers(monkeypatch)
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
        _patch_overlap_helpers(monkeypatch)
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
        _patch_overlap_helpers(monkeypatch)
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
        _patch_overlap_helpers(monkeypatch)
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
        _patch_overlap_helpers(monkeypatch)
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
        _patch_overlap_helpers(monkeypatch)
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
        _patch_overlap_helpers(monkeypatch)
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


def _insert_scoped_task(conn: sqlite3.Connection) -> int:
    """Insert a task whose description references a real path so
    task_referenced_paths returns a positive scope signal — required to
    exercise the Case B (file-overlap mismatch) branch of the issue #656
    override. The minimal _insert_task helper writes no description, which
    yields an empty scope signal and routes through the conservative branch.
    """
    cur = conn.execute(
        "INSERT INTO tasks (summary, description, status, task_type, priority, complexity, priority_score)"
        " VALUES ('test task with scope', 'Refactor apps/api/scrapers/palm_beach_improv.py to use the new base class.', 'In Progress', 'feature', 'Medium', 'S', 50)"
    )
    conn.commit()
    return cur.lastrowid


class TestPrefixMatchFalsePositive:
    """Issue #656: tusk merge must not skip ff-only and force-delete the feature
    branch when the 'commit on default' inference rests on absent or unrelated
    [TASK-N] commits.

    Two shapes covered:

    1. **Untagged feature-branch commit** (Case A) — the feature branch carries
       a commit whose message doesn't include `[TASK-X]` (e.g. a cherry-pick
       from another repo using a different prefix scheme). The branch-scoped
       log-check (`git log <branch> --not <default> --grep=\\[TASK-X\\]`) returns
       empty and `task_on_default` flips True. With no `[TASK-X]` commits on
       the default branch to validate against, the override resets
       `task_on_default` back to False and ff-merge proceeds. This is the exact
       shape of the original incident (TASK-1894 / `[club-379]` cherry-pick).

    2. **Unrelated [TASK-X] commits on default** (Case B) — `[TASK-X]` commits
       exist on the default branch but their file diff doesn't touch any path
       referenced in the task's description / criteria. The override flags it
       as a prefix-match false-positive and resets `task_on_default` to False.
    """

    def test_untagged_feature_branch_commit_proceeds_with_ff_merge(
        self, db_path, config_path, monkeypatch
    ):
        """Case A: feature branch has commits but none tagged [TASK-X], no [TASK-X]
        commits on default — override flips task_on_default back to False."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-untagged"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        # task_on_default=True → branch-scoped log returns empty (no [TASK-X] on
        # branch). cherry_pick_diverged=False → cherry returns '+' (commit not on
        # default by patch ID either).
        mock_run, _ = _make_run(
            branch, task_id=task_id, task_on_default=True,
            cherry_pick_diverged=False, record_calls=record,
        )
        # No [TASK-X] commits on default at all — Case A trigger.
        _patch_overlap_helpers(monkeypatch, matched_default_shas=[])
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert rc == 0, f"Expected exit 0\nstderr: {stderr_buf.getvalue()}"

        ff_calls = [c for c in record if c[:3] == ["git", "merge", "--ff-only"]]
        assert ff_calls, (
            "Expected git merge --ff-only to be called — feature branch's untagged "
            "commits must not be orphaned by the log-check's empty-branch inference"
        )

        force_delete_calls = [c for c in record if c[:3] == ["git", "branch", "-D"]]
        assert not force_delete_calls, (
            f"Expected NO force-delete on the false-positive path, got: {force_delete_calls}"
        )

        assert "issue #656" in stderr_buf.getvalue(), (
            "Expected the override's diagnostic note in stderr "
            f"(referencing issue #656):\n{stderr_buf.getvalue()}"
        )

    def test_unrelated_taskx_on_default_no_overlap_proceeds_with_ff_merge(
        self, db_path, config_path, monkeypatch
    ):
        """Case B: [TASK-X] commits exist on default but their file diff doesn't
        overlap with the task's referenced paths — override flips
        task_on_default back to False."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_scoped_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-scoped-fix"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _make_run(
            branch, task_id=task_id, task_on_default=True,
            cherry_pick_diverged=False, record_calls=record,
        )
        # [TASK-X] commits exist on default, but their file diff is unrelated to
        # the task's scope (apps/api/config/proxy_keys.json vs the task's
        # apps/api/scrapers/palm_beach_improv.py).
        _patch_overlap_helpers(
            monkeypatch,
            matched_default_shas=["7a2f1404" + "0" * 32],
            matched_files={"apps/api/config/proxy_keys.json"},
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
            "Expected git merge --ff-only to be called — unrelated [TASK-X] commits "
            "on default must not trigger the skip+delete path"
        )

        force_delete_calls = [c for c in record if c[:3] == ["git", "branch", "-D"]]
        assert not force_delete_calls, (
            f"Expected NO force-delete on the false-positive path, got: {force_delete_calls}"
        )

        assert "prefix-match false positive" in stderr_buf.getvalue(), (
            "Expected the override's prefix-match-false-positive diagnostic in "
            f"stderr:\n{stderr_buf.getvalue()}"
        )

    def test_high_confidence_path_logs_matched_shas(
        self, db_path, config_path, monkeypatch
    ):
        """When matched [TASK-X] commits exist on default and overlap with task
        scope (or task has no scope signal), the override keeps task_on_default
        True but logs the matched SHAs for operator visibility (criterion #4)."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)  # no scope signal → conservative path
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-high-confidence"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _make_run(
            branch, task_id=task_id, task_on_default=True,
            cherry_pick_diverged=False, record_calls=record,
        )
        _patch_overlap_helpers(
            monkeypatch,
            matched_default_shas=["abcdef12" + "0" * 32],
            matched_files={"some/file.py"},
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        ff_calls = [c for c in record if c[:3] == ["git", "merge", "--ff-only"]]
        assert not ff_calls, (
            "Expected git merge --ff-only NOT to be called on the high-confidence path"
        )

        # The matched-SHA log line is what lets operators eyeball mismatches
        # without git log archaeology (criterion #4).
        err = stderr_buf.getvalue()
        assert "matched [TASK-" in err and "abcdef1" in err, (
            f"Expected matched-commit SHA in stderr, got:\n{err}"
        )
