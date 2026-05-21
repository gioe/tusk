"""Regression tests for tusk merge when the default branch is locked by another worktree.

Issue #695: a linked worktree running on a feature branch cannot check out the
default branch when another worktree already has it checked out.  The merge path
should use a no-checkout fast-forward push instead of failing after closing the
session.
"""

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr, redirect_stdout

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


def _setup_task_session(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        task_id = _insert_task(conn)
        session_id = _insert_session(conn, task_id)
    finally:
        conn.close()
    return task_id, session_id


def _mock_run_factory(
    *,
    branch_name: str,
    task_id: int,
    default_branch: str = "main",
    has_origin: bool = True,
    default_locked: bool = True,
    dirty_file: str = "",
    no_checkout_push_succeeds: bool = True,
    branch_contains_origin: bool = True,
    has_remote_feature_upstream: bool = False,
    record_calls: list | None = None,
):
    calls = record_calls if record_calls is not None else []

    def _run(args, check=True):
        calls.append(list(args))

        if args[:2] == ["git", "diff"] and "--name-only" in args:
            return subprocess.CompletedProcess(args, 0, stdout=dirty_file, stderr="")
        if args[:3] == ["git", "stash", "push"]:
            return subprocess.CompletedProcess(
                args, 0, stdout="No local changes to save", stderr=""
            )
        if args[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(
                args,
                0 if has_origin else 2,
                stdout="git@example.com:owner/repo.git\n" if has_origin else "",
                stderr="" if has_origin else "fatal: No such remote 'origin'\n",
            )
        if args[:4] == ["git", "worktree", "list", "--porcelain"]:
            stdout = (
                f"worktree /tmp/repo-main\n"
                f"HEAD abc123\n"
                f"branch refs/heads/{default_branch}\n"
                if default_locked
                else ""
            )
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        if args[:2] == ["git", "checkout"] and args[2:3] == [default_branch]:
            return subprocess.CompletedProcess(
                args,
                128,
                stdout="",
                stderr=(
                    f"fatal: '{default_branch}' is already used by worktree at "
                    "'/tmp/repo-main'\n"
                ),
            )
        if (
            args[:3] == ["git", "rev-parse", "--verify"]
            and args[3:4] == [f"refs/remotes/origin/{default_branch}"]
        ):
            return subprocess.CompletedProcess(args, 0, stdout="abc123\n", stderr="")
        if (
            args[:2] == ["git", "log"]
            and any(
                a == f"refs/remotes/origin/{default_branch}..{default_branch}"
                for a in args
            )
        ):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "fetch", "origin"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(
                args,
                0 if branch_contains_origin else 1,
                stdout="",
                stderr="",
            )
        if args[:3] == ["git", "rebase", f"origin/{default_branch}"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "config", "--get"]:
            key = args[3]
            if key == f"branch.{branch_name}.remote":
                return subprocess.CompletedProcess(
                    args,
                    0 if has_remote_feature_upstream else 1,
                    stdout="origin\n" if has_remote_feature_upstream else "",
                    stderr="",
                )
            if key == f"branch.{branch_name}.merge":
                return subprocess.CompletedProcess(
                    args,
                    0 if has_remote_feature_upstream else 1,
                    stdout=f"refs/heads/{branch_name}\n"
                    if has_remote_feature_upstream
                    else "",
                    stderr="",
                )
        if args[:2] == ["git", "push"] and args[2:4] == [
            "origin",
            f"{branch_name}:{default_branch}",
        ]:
            return subprocess.CompletedProcess(
                args,
                0 if no_checkout_push_succeeds else 1,
                stdout="",
                stderr=(
                    ""
                    if no_checkout_push_succeeds
                    else "! [rejected] feature -> main (non-fast-forward)\n"
                ),
            )
        if args[:3] == ["git", "push", "origin"] and args[3:5] == [
            "--delete",
            branch_name,
        ]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if "session-close" in args:
            return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")
        if "task-done" in args:
            payload = json.dumps(
                {
                    "task": {
                        "id": task_id,
                        "status": "Done",
                        "closed_reason": "completed",
                    },
                    "sessions_closed": 0,
                    "unblocked_tasks": [],
                }
            )
            return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")

        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return _run, calls


class TestLinkedWorktreeDefaultBranchLocked:
    def test_uses_no_checkout_fast_forward_push(self, db_path, config_path, monkeypatch):
        task_id, session_id = _setup_task_session(db_path)
        branch = f"feature/TASK-{task_id}-worktree-lock"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _mock_run_factory(
            branch_name=branch,
            task_id=task_id,
            record_calls=record,
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert rc == 0, f"Expected exit 0\nstderr: {stderr_buf.getvalue()}"
        assert ["git", "push", "origin", f"{branch}:main"] in record
        assert not [c for c in record if c[:3] == ["git", "merge", "--ff-only"]]
        # No-checkout fast-forward path now deletes the local feature branch
        # after the push succeeds (issue #765). With no recorded workspace in
        # this test fixture, the cleanup helper falls through to the
        # branch-only delete using -D (the push has already shipped the
        # commits to origin, so -D is safe).
        assert [c for c in record if c[:3] == ["git", "branch", "-D"] and c[-1] == branch], (
            f"Expected git branch -D {branch} after no-checkout push success; got: {record}"
        )
        assert "no-checkout fast-forward" in stderr_buf.getvalue()

    def test_no_checkout_fetches_and_refuses_predictable_non_ff_before_push(
        self, db_path, config_path, monkeypatch
    ):
        task_id, session_id = _setup_task_session(db_path)
        branch = f"feature/TASK-{task_id}-worktree-lock"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _mock_run_factory(
            branch_name=branch,
            task_id=task_id,
            branch_contains_origin=False,
            record_calls=record,
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert rc == 2
        assert ["git", "fetch", "origin"] in record
        assert ["git", "merge-base", "--is-ancestor", "origin/main", branch] in record
        assert ["git", "push", "origin", f"{branch}:main"] not in record
        stderr = stderr_buf.getvalue()
        assert "origin/main has commits not reachable" in stderr
        assert f"tusk merge {task_id} --session {session_id} --rebase" in stderr
        assert not [c for c in record if "session-close" in c]
        assert not [c for c in record if "task-done" in c]

    def test_no_checkout_rebase_checks_out_feature_branch_before_rebase(
        self, db_path, config_path, monkeypatch
    ):
        task_id, session_id = _setup_task_session(db_path)
        branch = f"feature/TASK-{task_id}-worktree-lock"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _mock_run_factory(
            branch_name=branch,
            task_id=task_id,
            record_calls=record,
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

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

        assert rc == 0, f"Expected exit 0\nstderr: {stderr_buf.getvalue()}"
        checkout_idx = next(
            (i for i, c in enumerate(record) if c == ["git", "checkout", branch]),
            None,
        )
        rebase_idx = next(
            (i for i, c in enumerate(record) if c == ["git", "rebase", "origin/main"]),
            None,
        )
        push_idx = next(
            (i for i, c in enumerate(record) if c == ["git", "push", "origin", f"{branch}:main"]),
            None,
        )

        assert checkout_idx is not None, "Expected no-checkout --rebase to checkout the task branch"
        assert rebase_idx is not None, "Expected no-checkout --rebase to run git rebase origin/main"
        assert push_idx is not None, "Expected no-checkout --rebase to push the task branch"
        assert checkout_idx < rebase_idx < push_idx

    def test_no_checkout_success_deletes_remote_feature_upstream(
        self, db_path, config_path, monkeypatch
    ):
        task_id, session_id = _setup_task_session(db_path)
        branch = f"feature/TASK-{task_id}-worktree-lock"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _mock_run_factory(
            branch_name=branch,
            task_id=task_id,
            has_remote_feature_upstream=True,
            record_calls=record,
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert rc == 0, f"Expected exit 0\nstderr: {stderr_buf.getvalue()}"
        push_idx = record.index(["git", "push", "origin", f"{branch}:main"])
        delete_idx = record.index(["git", "push", "origin", "--delete", branch])
        assert push_idx < delete_idx
        assert "Deleted remote feature branch origin/" in stderr_buf.getvalue()

    def test_rebase_before_no_checkout_fast_forward_push(
        self, db_path, config_path, monkeypatch
    ):
        task_id, session_id = _setup_task_session(db_path)
        branch = f"feature/TASK-{task_id}-worktree-lock"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _mock_run_factory(
            branch_name=branch,
            task_id=task_id,
            record_calls=record,
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

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

        assert rc == 0, f"Expected exit 0\nstderr: {stderr_buf.getvalue()}"
        fetch_idx = record.index(["git", "fetch", "origin"])
        rebase_idx = record.index(["git", "rebase", "origin/main"])
        push_idx = record.index(["git", "push", "origin", f"{branch}:main"])
        assert fetch_idx < rebase_idx < push_idx
        assert "Rebasing" in stderr_buf.getvalue()
        assert "origin/main" in stderr_buf.getvalue()

    def test_no_checkout_push_rejects_non_fast_forward(
        self, db_path, config_path, monkeypatch
    ):
        task_id, session_id = _setup_task_session(db_path)
        branch = f"feature/TASK-{task_id}-worktree-lock"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _mock_run_factory(
            branch_name=branch,
            task_id=task_id,
            no_checkout_push_succeeds=False,
            record_calls=record,
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert rc == 2
        stderr = stderr_buf.getvalue()
        assert "non-fast-forward" in stderr
        assert f"git fetch origin && git rebase origin/main" in stderr
        assert ["git", "push", "origin", f"{branch}:main"] in record
        assert not [c for c in record if "session-close" in c]
        assert not [c for c in record if "task-done" in c]

    def test_locked_default_without_origin_does_not_close_session(
        self, db_path, config_path, monkeypatch
    ):
        task_id, session_id = _setup_task_session(db_path)
        branch = f"feature/TASK-{task_id}-worktree-lock"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _mock_run_factory(
            branch_name=branch,
            task_id=task_id,
            has_origin=False,
            record_calls=record,
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert rc == 2
        stderr = stderr_buf.getvalue()
        assert "no git remote 'origin'" in stderr
        assert not [c for c in record if "session-close" in c]
        assert not [c for c in record if "task-done" in c]

    def test_locked_default_without_origin_does_not_stash_dirty_tree_before_failing(
        self, db_path, config_path, monkeypatch
    ):
        task_id, session_id = _setup_task_session(db_path)
        branch = f"feature/TASK-{task_id}-worktree-lock"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _mock_run_factory(
            branch_name=branch,
            task_id=task_id,
            has_origin=False,
            dirty_file="unrelated-task-file.py\n",
            record_calls=record,
        )
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert rc == 2
        stderr = stderr_buf.getvalue()
        assert "/tmp/repo-main" in stderr
        assert "no git remote 'origin'" in stderr
        assert not [c for c in record if c[:3] == ["git", "stash", "push"]]
        assert not [c for c in record if "session-close" in c]
        assert not [c for c in record if "task-done" in c]
