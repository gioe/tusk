"""Tests for tusk merge --rebase flag (TASK-691 / issue #392).

Covers:
- --rebase flag accepted without error
- Rebase succeeds: git rebase and checkout calls made in correct order, ff-only merge proceeds
- Rebase fails: git rebase --abort called, error message includes resolution steps, exits non-zero
- ff-only merge error message (without --rebase) includes exact rebase commands
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


def _make_base_run(
    branch_name: str,
    default_branch: str = "main",
    rebase_rc: int = 0,
    task_id: int = 1,
    record_calls: list | None = None,
):
    """Return a mock run() for the local-merge path with optional --rebase support.

    rebase_rc: exit code returned by 'git rebase'; 0 = success, non-zero = conflict.
    record_calls: if provided, every args list is appended to this list.
    """
    calls = record_calls if record_calls is not None else []

    def _run(args, check=True):
        calls.append(list(args))
        cmd = args[:3] if len(args) >= 3 else args

        if args[:2] == ["git", "diff"] and "--name-only" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "stash", "push"]:
            return subprocess.CompletedProcess(args, 0, stdout="No local changes to save", stderr="")
        if args[:2] == ["git", "checkout"] and len(args) == 3:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "pull", "origin"] or ("pull" in args and "origin" in args):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        # Branch-scoped stale-commit detection: git log <branch> --not <default> --grep=\[TASK-N\]
        # Return the feature branch's own task commit (non-empty) so task_on_default=False
        # and the rebase/ff-merge path is not skipped.
        if args[:2] == ["git", "log"] and any(f"--grep=\\[TASK-{task_id}\\]" in a for a in args):
            return subprocess.CompletedProcess(
                args, 0, stdout=f"abc1234 [TASK-{task_id}] implement fix\n", stderr=""
            )
        if args[:3] == ["git", "rebase", default_branch]:
            return subprocess.CompletedProcess(args, rebase_rc, stdout="", stderr="CONFLICT (content)" if rebase_rc != 0 else "")
        if args[:3] == ["git", "rebase", "--abort"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "merge", "--ff-only"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "branch", "-d"]:
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


class TestRebaseFlagParsing:
    """--rebase is accepted without error and sets use_rebase=True."""

    def test_rebase_flag_unknown_arg_not_raised(self, db_path, config_path, monkeypatch):
        """--rebase is not rejected as an unknown argument."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: ("feature/TASK-1-x", None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _make_base_run("feature/TASK-1-x", task_id=task_id)
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main([str(db_path), str(config_path), str(task_id),
                                  "--session", str(session_id), "--rebase"])

        assert rc == 0, f"Expected exit 0, got {rc}\nstderr: {stderr_buf.getvalue()}"
        stderr = stderr_buf.getvalue()
        assert "Unknown argument" not in stderr


class TestRebaseSuccess:
    """When --rebase is passed and rebase succeeds, the correct git calls are made."""

    def test_rebase_checkout_sequence(self, db_path, config_path, monkeypatch):
        """With --rebase: checkout feature → rebase → checkout default → ff-only merge."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-my-branch"
        default = "main"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: default)
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _make_base_run(branch, default_branch=default, rebase_rc=0,
                                     task_id=task_id, record_calls=record)
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main([str(db_path), str(config_path), str(task_id),
                                  "--session", str(session_id), "--rebase"])

        assert rc == 0, f"Expected exit 0\nstderr: {stderr_buf.getvalue()}"

        # Extract git checkout calls in order
        checkout_calls = [c for c in record if c[:2] == ["git", "checkout"]]
        rebase_calls = [c for c in record if c[:2] == ["git", "rebase"] and c[2:3] != ["--abort"]]
        ff_calls = [c for c in record if c[:3] == ["git", "merge", "--ff-only"]]

        assert rebase_calls, "Expected git rebase to be called"
        assert any(branch in c for c in checkout_calls), "Expected checkout to feature branch"
        assert ff_calls, "Expected ff-only merge to be called"

        # checkout feature branch must precede the rebase
        rebase_idx = next(i for i, c in enumerate(record) if c[:2] == ["git", "rebase"] and c[2:3] != ["--abort"])
        co_feature_idx = next(
            (i for i, c in enumerate(record) if c[:2] == ["git", "checkout"] and c[2:3] == [branch]),
            None,
        )
        assert co_feature_idx is not None, "Expected checkout of feature branch before rebase"
        assert co_feature_idx < rebase_idx, "Checkout of feature branch must precede rebase"

        # checkout default branch must follow the rebase and precede ff-only merge
        ff_idx = next(i for i, c in enumerate(record) if c[:3] == ["git", "merge", "--ff-only"])
        co_default_after_rebase = [
            i for i, c in enumerate(record)
            if c[:2] == ["git", "checkout"] and c[2:3] == [default] and i > rebase_idx and i < ff_idx
        ]
        assert co_default_after_rebase, "Expected checkout of default branch after rebase and before ff-only merge"


class TestRebaseFailure:
    """When --rebase is passed and rebase fails, git rebase --abort is called and exit is non-zero."""

    def test_rebase_conflict_exits_nonzero(self, db_path, config_path, monkeypatch):
        """Rebase conflict: exits 2, calls git rebase --abort, stderr includes resolution steps."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-my-branch"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _make_base_run(branch, rebase_rc=1, task_id=task_id, record_calls=record)
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main([str(db_path), str(config_path), str(task_id),
                                  "--session", str(session_id), "--rebase"])

        assert rc == 2, f"Expected exit 2 on rebase conflict, got {rc}"

        stderr = stderr_buf.getvalue()
        assert "rebase" in stderr.lower(), "Expected rebase-related message in stderr"
        assert "rebase --abort" in stderr, "Expected git rebase --abort instruction in stderr"
        assert "git rebase --continue" in stderr, "Expected git rebase --continue instruction"

        abort_calls = [c for c in record if c[:3] == ["git", "rebase", "--abort"]]
        assert abort_calls, "Expected git rebase --abort to be called on conflict"

    def test_rebase_conflict_no_ff_merge(self, db_path, config_path, monkeypatch):
        """When rebase fails, ff-only merge is NOT attempted."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-my-branch"
        record = []

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        mock_run, _ = _make_base_run(branch, rebase_rc=1, task_id=task_id, record_calls=record)
        monkeypatch.setattr(tusk_merge, "run", mock_run)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            tusk_merge.main([str(db_path), str(config_path), str(task_id),
                             "--session", str(session_id), "--rebase"])

        ff_calls = [c for c in record if c[:3] == ["git", "merge", "--ff-only"]]
        assert not ff_calls, "ff-only merge must not be attempted after rebase failure"


class TestFfOnlyErrorMessage:
    """When ff-only merge fails without --rebase, error message includes exact rebase commands."""

    def test_ff_only_error_includes_rebase_commands(self, db_path, config_path, monkeypatch):
        """Error message for ff-only failure includes the exact git rebase and tusk merge --rebase commands."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch = f"feature/TASK-{task_id}-my-branch"

        def _mock_run(args, check=True):
            if args[:2] == ["git", "diff"] and "--name-only" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:2] == ["git", "stash", "push"]:
                return subprocess.CompletedProcess(args, 0, stdout="No local changes to save", stderr="")
            if args[:2] == ["git", "checkout"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if "pull" in args and "origin" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            # Branch-scoped log: return non-empty so task_on_default=False and ff-merge is attempted
            if args[:2] == ["git", "log"] and any("--grep=" in a for a in args):
                return subprocess.CompletedProcess(
                    args, 0, stdout=f"abc1234 [TASK-{task_id}] implement fix\n", stderr=""
                )
            if args[:3] == ["git", "merge", "--ff-only"]:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="fatal: Not possible to fast-forward")
            if "session-close" in args:
                return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_merge, "find_task_branch", lambda tid: (branch, None, False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)
        monkeypatch.setattr(tusk_merge, "run", _mock_run)

        stderr_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
            rc = tusk_merge.main([str(db_path), str(config_path), str(task_id),
                                  "--session", str(session_id)])

        assert rc == 2, f"Expected exit 2 on ff-only failure, got {rc}"

        stderr = stderr_buf.getvalue()
        assert "git rebase origin/main" in stderr, \
            f"Expected 'git rebase origin/main' in error message:\n{stderr}"
        assert "--rebase" in stderr, \
            f"Expected '--rebase' flag mentioned in error message:\n{stderr}"
