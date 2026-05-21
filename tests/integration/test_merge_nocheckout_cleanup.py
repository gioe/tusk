"""Regression test for issue #765.

After a successful no-checkout fast-forward push, ``tusk merge`` must
also remove the recorded task worktree, delete the local feature branch,
and clear the ``task_workspaces`` row — otherwise these accumulate across
many tasks (the user observed 9 stale rows in one project) even though
the commits have already shipped to origin/<default>.
"""

import importlib.util
import io
import json
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


class TestNoCheckoutCleanupHelper:
    """Unit tests for _cleanup_no_checkout_workspace's branches."""

    def test_no_recorded_workspace_falls_back_to_branch_delete(
        self, db_path, monkeypatch
    ):
        """No recorded workspace -> still attempts git branch -D as best-effort."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
        finally:
            conn.close()

        calls = []

        def _mock_run(args, check=True):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _mock_run)

        tusk_merge._cleanup_no_checkout_workspace(
            str(db_path), task_id, f"feature/TASK-{task_id}-x"
        )

        assert calls == [
            ["git", "branch", "-D", f"feature/TASK-{task_id}-x"]
        ], f"Expected exactly one git branch -D call; got: {calls}"

    def test_branch_delete_failure_surfaces_warning(self, db_path, monkeypatch, capsys):
        """When git branch -D fails, the warning names the branch and the manual recovery."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
        finally:
            conn.close()
        branch = f"feature/TASK-{task_id}-y"

        def _mock_run(args, check=True):
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="error: branch not found\n"
            )

        monkeypatch.setattr(tusk_merge, "run", _mock_run)

        tusk_merge._cleanup_no_checkout_workspace(str(db_path), task_id, branch)
        captured = capsys.readouterr()
        assert "Warning: git branch -D" in captured.err
        assert branch in captured.err


class TestNoCheckoutCleanupRemovesRecordedWorkspace:
    """Recorded workspace case: chdir out, worktree-remove, branch-delete, row-clear."""

    def test_full_cleanup_path_invokes_each_step(self, db_path, monkeypatch, tmp_path):
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
        finally:
            conn.close()
        branch = f"feature/TASK-{task_id}-recorded"
        workspace_path = tmp_path / "workspace"
        workspace_path.mkdir()

        conn = sqlite3.connect(str(db_path))
        try:
            workspace_id = _insert_workspace(
                conn, task_id, branch, str(workspace_path)
            )
        finally:
            conn.close()

        # Track chdir + each git call
        chdir_calls = []
        real_chdir = os.chdir

        def _chdir_spy(path):
            chdir_calls.append(str(path))
            real_chdir(str(path))

        monkeypatch.setattr(tusk_merge.os, "chdir", _chdir_spy)

        git_calls = []

        def _mock_run(args, check=True):
            git_calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _mock_run)

        tusk_merge._cleanup_no_checkout_workspace(str(db_path), task_id, branch)

        # 1. chdir to repo root
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(str(db_path))))
        assert any(repo_root in call for call in chdir_calls), (
            f"Expected chdir to repo root {repo_root}; got: {chdir_calls}"
        )

        # 2. git worktree remove
        assert any(
            cmd[:3] == ["git", "worktree", "remove"]
            and str(workspace_path) in cmd
            for cmd in git_calls
        ), f"Expected git worktree remove {workspace_path}; got: {git_calls}"

        # 3. git branch -D
        assert [
            "git",
            "branch",
            "-D",
            branch,
        ] in git_calls, f"Expected git branch -D {branch}; got: {git_calls}"

        # 4. task_workspaces row cleared
        with sqlite3.connect(str(db_path)) as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM task_workspaces WHERE id = ?",
                (workspace_id,),
            ).fetchone()[0]
        assert remaining == 0, (
            f"Expected task_workspaces row {workspace_id} to be deleted; "
            f"{remaining} row(s) remain."
        )
