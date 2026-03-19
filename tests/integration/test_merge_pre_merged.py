"""Regression tests for tusk merge pre-merged branch detection (TASK-683 / issue #385).

When 'tusk merge' is called and the user is already on the default branch with no
feature branch present (branch was previously merged and deleted), find_task_branch
should return (None, None, True) and main() should auto-complete finalization instead
of exiting with an error.
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


# ---------------------------------------------------------------------------
# find_task_branch pre-merged detection
# ---------------------------------------------------------------------------


class TestFindTaskBranchPreMerged:
    """Unit tests for find_task_branch's pre-merged detection logic."""

    def _make_run(self, *, on_default: bool, default_branch: str = "main"):
        """Return a mock run() and detect_default_branch for find_task_branch."""
        def _run(args, check=True):
            if args[:3] == ["git", "branch", "--list"]:
                # No feature branches
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                branch = default_branch if on_default else "feature/TASK-99-something"
                return subprocess.CompletedProcess(args, 0, stdout=branch + "\n", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        return _run

    def test_pre_merged_returns_true_when_on_default(self, monkeypatch):
        """Returns (None, None, True) when on default branch with no feature branch."""
        monkeypatch.setattr(tusk_merge, "run", self._make_run(on_default=True))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")

        branch, err, pre_merged = tusk_merge.find_task_branch(42)

        assert branch is None
        assert err is None
        assert pre_merged is True

    def test_pre_merged_false_when_not_on_default(self, monkeypatch):
        """Returns (None, error_msg, False) when NOT on default branch."""
        monkeypatch.setattr(tusk_merge, "run", self._make_run(on_default=False))
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")

        branch, err, pre_merged = tusk_merge.find_task_branch(42)

        assert branch is None
        assert err is not None
        assert "No branch found matching" in err
        assert pre_merged is False

    def test_pre_merged_false_when_branch_exists(self, monkeypatch):
        """Returns (branch_name, None, False) when feature branch is present."""
        def _run(args, check=True):
            if args[:3] == ["git", "branch", "--list"]:
                return subprocess.CompletedProcess(
                    args, 0, stdout="  feature/TASK-42-my-branch\n", stderr=""
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _run)

        branch, err, pre_merged = tusk_merge.find_task_branch(42)

        assert branch == "feature/TASK-42-my-branch"
        assert err is None
        assert pre_merged is False


# ---------------------------------------------------------------------------
# main() auto-complete path (integration)
# ---------------------------------------------------------------------------


class TestMergePreMergedAutoComplete:
    """main() auto-completes when find_task_branch returns pre_merged=True."""

    def test_auto_complete_exits_zero(self, db_path, config_path, monkeypatch, tmp_path):
        """main() exits 0 and marks task Done via the pre-merged auto-complete path."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        tusk_bin = os.path.join(REPO_ROOT, "bin", "tusk")

        # Simulate: on default branch, no feature branch
        monkeypatch.setattr(
            tusk_merge,
            "find_task_branch",
            lambda tid: (None, None, True),
        )
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "checkpoint_wal", lambda db: None)

        git_calls = []

        def _mock_run(args, check=True):
            git_calls.append(list(args))
            tusk_cmd = args[0] if args else ""
            if "session-close" in args:
                return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")
            if "push" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if "task-done" in args:
                result_json = json.dumps({
                    "task": {"id": task_id, "status": "Done", "closed_reason": "completed"},
                    "sessions_closed": 0,
                    "unblocked_tasks": [],
                })
                return subprocess.CompletedProcess(args, 0, stdout=result_json, stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _mock_run)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exit_code = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert exit_code == 0, f"Expected exit 0, got {exit_code}\nstderr: {stderr_buf.getvalue()}"

        stderr_out = stderr_buf.getvalue()
        assert "previously merged" in stderr_out, f"Expected 'previously merged' note in stderr:\n{stderr_out}"

        # Verify push was attempted
        push_calls = [c for c in git_calls if c[:2] == ["git", "push"]]
        assert push_calls, "Expected git push to be called in auto-complete path"

        # Verify task-done was called
        task_done_calls = [c for c in git_calls if "task-done" in c]
        assert task_done_calls, "Expected task-done to be called in auto-complete path"

        # Verify JSON output contains the task result
        stdout_out = stdout_buf.getvalue()
        result = json.loads(stdout_out)
        assert result["task"]["status"] == "Done"
        assert result["sessions_closed"] == 1
