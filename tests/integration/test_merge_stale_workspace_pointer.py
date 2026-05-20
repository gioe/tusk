"""Regression test for issue #763.

Previously, ``tusk merge`` honored a recorded ``task_workspaces`` branch
pointer even when that branch contained zero ``[TASK-<id>]`` commits ahead
of the default branch — so an abandoned-session slug silently won over a
later ``feature/TASK-<id>-<new-slug>`` branch that actually carried the
user's work. The fix validates the recorded pointer has task commits before
honoring it, and falls back to the commit-pattern scan in
``find_task_branch`` otherwise.
"""

import importlib.util
import io
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr, redirect_stdout

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


def _insert_workspace(conn, task_id, branch, workspace_path):
    cur = conn.execute(
        "INSERT INTO task_workspaces (task_id, branch, workspace_path)"
        " VALUES (?, ?, ?)",
        (task_id, branch, workspace_path),
    )
    conn.commit()
    return cur.lastrowid


class TestStaleRecordedWorkspaceFallsBack:
    """A recorded pointer with no [TASK-N] commits is treated as stale."""

    def _setup(self, db_path, task_id, monkeypatch, *, stale_path_missing=False):
        stale_branch = f"feature/TASK-{task_id}-abandoned-slug"
        real_branch = f"feature/TASK-{task_id}-real-slug"
        stale_path = "/tmp/does/not/exist" if stale_path_missing else os.path.join(
            os.path.dirname(str(db_path)), "stale-worktree"
        )

        conn = sqlite3.connect(str(db_path))
        try:
            _insert_workspace(conn, task_id, stale_branch, stale_path)
        finally:
            conn.close()

        recorded_commits = {
            stale_branch: "",
            real_branch: "abc123\n",
        }

        def _mock_run(args, check=True):
            if args[:2] == ["git", "show-ref"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:3] == ["git", "branch", "--list"]:
                return subprocess.CompletedProcess(
                    args, 0, stdout=f"  {stale_branch}\n  {real_branch}\n", stderr=""
                )
            if (
                args[:2] == ["git", "log"]
                and any(isinstance(a, str) and a.startswith("--grep=") for a in args)
            ):
                branch = next(
                    (
                        a.split("..")[1]
                        for a in args
                        if isinstance(a, str) and ".." in a
                    ),
                    "",
                )
                stdout = recorded_commits.get(branch, "")
                return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _mock_run)
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        return stale_branch, real_branch, stale_path

    def test_stale_pointer_falls_back_to_real_branch(self, db_path, monkeypatch):
        """Recorded stale branch + real feature branch -> real branch selected."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            _insert_session(conn, task_id)
        finally:
            conn.close()

        stale_branch, real_branch, _ = self._setup(db_path, task_id, monkeypatch)

        captured_branch = None
        original_resolve = tusk_merge._recorded_task_workspace

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            recorded = original_resolve(str(db_path), task_id)
            candidate_branch = recorded["branch"]
            default_branch = tusk_merge.detect_default_branch()
            has_task_commits = (
                tusk_merge._branch_exists(candidate_branch)
                and tusk_merge._branch_has_task_commits(
                    candidate_branch, task_id, default_branch
                )
            )
            if has_task_commits:
                captured_branch = candidate_branch
            else:
                captured_branch, err, _ = tusk_merge.find_task_branch(task_id)

        assert captured_branch == real_branch, (
            f"Expected {real_branch} (the real, commit-bearing branch), "
            f"got {captured_branch}. The stale recorded pointer {stale_branch} "
            "should have been rejected."
        )

    def test_stale_pointer_emits_warning(self, db_path, monkeypatch, capsys):
        """Stale pointer fallback prints a Warning identifying the stale signal."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
        finally:
            conn.close()

        stale_branch, real_branch, _ = self._setup(
            db_path, task_id, monkeypatch, stale_path_missing=True
        )

        recorded = tusk_merge._recorded_task_workspace(str(db_path), task_id)
        candidate_branch = recorded["branch"]
        default_branch = tusk_merge.detect_default_branch()
        branch_exists = tusk_merge._branch_exists(candidate_branch)
        has_task_commits = branch_exists and tusk_merge._branch_has_task_commits(
            candidate_branch, task_id, default_branch
        )
        path_exists = os.path.exists(recorded["workspace_path"])

        assert branch_exists is True
        assert has_task_commits is False, (
            "Stale branch carries no [TASK-N] commits — _branch_has_task_commits "
            "should return False so main() falls back."
        )
        assert path_exists is False, (
            "Workspace path under /tmp/does/not/exist is expected to be missing."
        )

    def test_branch_has_task_commits_detects_real_branch(self, db_path, monkeypatch):
        """_branch_has_task_commits returns True when the branch has [TASK-N] commits."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
        finally:
            conn.close()

        _, real_branch, _ = self._setup(db_path, task_id, monkeypatch)

        assert tusk_merge._branch_has_task_commits(
            real_branch, task_id, "main"
        ) is True

    def test_branch_has_task_commits_rejects_stale_branch(self, db_path, monkeypatch):
        """_branch_has_task_commits returns False when the branch has no [TASK-N] commits."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
        finally:
            conn.close()

        stale_branch, _, _ = self._setup(db_path, task_id, monkeypatch)

        assert tusk_merge._branch_has_task_commits(
            stale_branch, task_id, "main"
        ) is False


class TestMultipleBranchesWithTaskCommitsConflict:
    """Two branches each with [TASK-N] commits -> conflict error, no silent pick."""

    def test_conflict_error_lists_all_candidates(self, monkeypatch):
        b1 = "feature/TASK-50-alpha"
        b2 = "feature/TASK-50-beta"

        def _mock_run(args, check=True):
            if args[:3] == ["git", "branch", "--list"]:
                return subprocess.CompletedProcess(
                    args, 0, stdout=f"  {b1}\n  {b2}\n", stderr=""
                )
            if args[:2] == ["git", "log"]:
                # Both branches have a [TASK-50] commit
                return subprocess.CompletedProcess(args, 0, stdout="abc\n", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _mock_run)
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")

        branch, err, pre_merged = tusk_merge.find_task_branch(50)

        assert branch is None
        assert err is not None
        assert "each containing [TASK-50] commits" in err
        assert b1 in err
        assert b2 in err
        assert pre_merged is False
