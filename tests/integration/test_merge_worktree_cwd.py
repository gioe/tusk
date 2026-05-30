"""Regression test for issue #764.

When the operator invokes ``tusk merge`` from a CWD that is NOT on the
default branch and a fresh recorded task workspace exists for the task on
a separate worktree, ``tusk merge`` switches CWD into the recorded
workspace so the rebase/push/branch-delete steps operate on the feature
branch's worktree — not on the (possibly dirty) primary repo whose
unstaged changes would otherwise blow up rebase with a misleading
"cannot rebase: You have unstaged changes" error.
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


class TestMergeChdirsIntoRecordedWorkspace:
    def _setup(self, db_path, monkeypatch, tmp_path, *, primary_branch):
        """Set up: real on-disk recorded workspace, mocked git ops."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        # Make the recorded workspace path actually exist on disk so
        # path_exists check in main() returns True.
        workspace_path = tmp_path / "workspace"
        workspace_path.mkdir()
        feature_branch = f"feature/TASK-{task_id}-recorded"

        conn = sqlite3.connect(str(db_path))
        try:
            _insert_workspace(conn, task_id, feature_branch, str(workspace_path))
        finally:
            conn.close()

        # Track every call to os.chdir so the test can assert which path
        # the merge selected.
        chdir_calls = []
        real_chdir = os.chdir

        def _chdir_spy(path):
            chdir_calls.append(str(path))
            real_chdir(str(path))

        monkeypatch.setattr(tusk_merge.os, "chdir", _chdir_spy)

        def _mock_run(args, check=True):
            # _branch_exists: return success so the recorded pointer is "fresh".
            if args[:2] == ["git", "show-ref"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            # _branch_has_task_commits: return a sha so the pointer passes
            # commit-presence validation.
            if (
                args[:2] == ["git", "log"]
                and any(isinstance(a, str) and a.startswith("--grep=") for a in args)
            ):
                return subprocess.CompletedProcess(args, 0, stdout="deadbeef\n", stderr="")
            # rev-parse --abbrev-ref HEAD: returns the primary's branch so the
            # chdir gate can compare it to default_branch.
            if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                return subprocess.CompletedProcess(args, 0, stdout=primary_branch + "\n", stderr="")
            # Any other git/tusk subcommand: short-circuit with a generic
            # failure so we don't actually try to merge — main() will exit
            # well after the chdir decision was made.
            if args[:2] == ["git", "status"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="(mocked failure)")

        monkeypatch.setattr(tusk_merge, "run", _mock_run)
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)

        return task_id, session_id, workspace_path, chdir_calls

    def test_chdir_when_cwd_not_on_default(
        self, db_path, config_path, monkeypatch, tmp_path
    ):
        """Primary CWD is on a non-default branch → tusk merge chdir's into recorded workspace."""
        task_id, session_id, workspace_path, chdir_calls = self._setup(
            db_path, monkeypatch, tmp_path, primary_branch="feature/TASK-99-other"
        )

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            tusk_merge.main(
                [
                    str(db_path),
                    str(config_path),
                    str(task_id),
                    "--session",
                    str(session_id),
                ]
            )

        assert any(
            str(workspace_path) in call for call in chdir_calls
        ), (
            f"Expected chdir into recorded workspace {workspace_path}, "
            f"got chdir calls: {chdir_calls}"
        )
        assert "switched CWD" in stderr_buf.getvalue(), (
            f"Expected switched-CWD Note in stderr:\n{stderr_buf.getvalue()}"
        )

    def test_no_chdir_when_cwd_already_on_default(
        self, db_path, config_path, monkeypatch, tmp_path
    ):
        """Primary CWD is on the default branch → keep CWD, do the local ff-only merge."""
        task_id, session_id, workspace_path, chdir_calls = self._setup(
            db_path, monkeypatch, tmp_path, primary_branch="main"
        )

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            tusk_merge.main(
                [
                    str(db_path),
                    str(config_path),
                    str(task_id),
                    "--session",
                    str(session_id),
                ]
            )

        assert not any(
            str(workspace_path) in call for call in chdir_calls
        ), (
            "Expected NO chdir into the recorded workspace when CWD is already "
            f"on the default branch. chdir calls: {chdir_calls}"
        )

    def test_missing_recorded_workspace_refuses_before_feature_checkout(
        self, db_path, config_path, monkeypatch, tmp_path
    ):
        """Manual worktree removal leaves a stale row; retry must not switch primary to feature."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
            missing_workspace = tmp_path / "removed-workspace"
            branch = f"feature/TASK-{task_id}-removed"
            _insert_workspace(conn, task_id, branch, str(missing_workspace))
        finally:
            conn.close()

        commands = []

        def _mock_run(args, check=True):
            commands.append(list(args))
            if args[:2] == ["git", "show-ref"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if (
                args[:2] == ["git", "log"]
                and any(isinstance(a, str) and a.startswith("--grep=") for a in args)
            ):
                return subprocess.CompletedProcess(args, 0, stdout="deadbeef\n", stderr="")
            if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")
            if args[:2] == ["git", "checkout"] and args[2:3] == [branch]:
                raise AssertionError(
                    "merge retry must not checkout the task feature branch in primary"
                )
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="(mocked failure)")

        monkeypatch.setattr(tusk_merge, "run", _mock_run)
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [
                    str(db_path),
                    str(config_path),
                    str(task_id),
                    "--session",
                    str(session_id),
                    "--rebase",
                ]
            )

        assert rc == 2
        stderr = stderr_buf.getvalue()
        assert "recorded task workspace path is missing" in stderr
        assert "git worktree remove --force" in stderr
        assert ["git", "checkout", branch] not in commands

    def test_cleanup_relocates_before_removing_current_worktree(
        self, db_path, monkeypatch, tmp_path
    ):
        """Deleting the invoking worktree must not leave later finalize calls without a CWD."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            branch = f"feature/TASK-{task_id}-cleanup"
            workspace = tmp_path / "workspace"
            nested = workspace / "nested"
            nested.mkdir(parents=True)
            _insert_workspace(conn, task_id, branch, str(workspace))
        finally:
            conn.close()

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(str(db_path))))
        monkeypatch.chdir(nested)
        monkeypatch.setattr(tusk_merge, "_clean_tusk_auto_symlinks", lambda *args: 0)

        remove_seen = False

        def _mock_run(args, check=True):
            nonlocal remove_seen
            if args[:3] == ["git", "-C", str(workspace)]:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
            if args == ["git", "worktree", "remove", str(workspace)]:
                remove_seen = True
                assert os.path.realpath(os.getcwd()) == os.path.realpath(repo_root)
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            raise AssertionError(f"unexpected run call: {args}")

        monkeypatch.setattr(tusk_merge, "run", _mock_run)

        assert tusk_merge._remove_recorded_task_worktree(
            str(db_path), task_id, branch
        )
        assert remove_seen

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT 1 FROM task_workspaces WHERE task_id = ?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row is None
