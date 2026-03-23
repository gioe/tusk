"""Unit tests for find_task_branch multi-branch selection (TASK-21).

When multiple branches match feature/TASK-N-*, tusk-merge should pick the
branch with the most recent tip commit rather than hard-failing.
"""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MERGE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-merge.py")


def _load_module():
    tusk_loader_mock = MagicMock()
    db_lib_mock = MagicMock()
    db_lib_mock.get_connection = MagicMock()
    db_lib_mock.checkpoint_wal = MagicMock()
    tusk_loader_mock.load.return_value = db_lib_mock
    with patch.dict("sys.modules", {"tusk_loader": tusk_loader_mock}):
        spec = importlib.util.spec_from_file_location("tusk_merge", MERGE_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _cp(returncode, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestFindTaskBranchMultiple:
    """Tests for find_task_branch when multiple branches match."""

    def test_selects_most_recent_branch(self, capsys):
        """Two branches → selects the one with the newer commit timestamp."""
        mod = _load_module()
        stale = "feature/TASK-5-old-work"
        current = "feature/TASK-5-new-work"

        timestamps = {
            stale: "1700000000",
            current: "1700001000",  # newer
        }

        def fake_run(args, check=True):
            if args[:3] == ["git", "branch", "--list"]:
                return _cp(0, stdout=f"  {stale}\n* {current}\n")
            if args[:3] == ["git", "log", "-1"]:
                branch = args[-1]
                return _cp(0, stdout=timestamps.get(branch, "0"))
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            branch, err, pre_merged = mod.find_task_branch(5)

        assert branch == current
        assert err is None
        assert pre_merged is False
        captured = capsys.readouterr()
        assert "Selecting most-recent-commit branch" in captured.err
        assert stale in captured.err

    def test_tie_returns_error(self, capsys):
        """Two branches with identical timestamps → error message, not hard selection."""
        mod = _load_module()
        b1 = "feature/TASK-7-alpha"
        b2 = "feature/TASK-7-beta"

        def fake_run(args, check=True):
            if args[:3] == ["git", "branch", "--list"]:
                return _cp(0, stdout=f"  {b1}\n  {b2}\n")
            if args[:3] == ["git", "log", "-1"]:
                return _cp(0, stdout="1700000000")  # same for both
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            branch, err, pre_merged = mod.find_task_branch(7)

        assert branch is None
        assert err is not None
        assert "equal recency" in err
        assert b1 in err
        assert b2 in err

    def test_single_branch_unchanged(self):
        """Single branch → original behavior, no stderr note."""
        mod = _load_module()
        only = "feature/TASK-3-the-branch"

        def fake_run(args, check=True):
            if args[:3] == ["git", "branch", "--list"]:
                return _cp(0, stdout=f"  {only}\n")
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            branch, err, pre_merged = mod.find_task_branch(3)

        assert branch == only
        assert err is None
        assert pre_merged is False

    def test_no_branch_returns_error(self):
        """No matching branch, not on default branch → error."""
        mod = _load_module()

        def fake_run(args, check=True):
            if args[:3] == ["git", "branch", "--list"]:
                return _cp(0, stdout="")
            if args[:2] == ["git", "rev-parse"]:
                return _cp(0, stdout="feature/TASK-9-something")
            return _cp(0, stdout="main")

        with patch.object(mod, "run", side_effect=fake_run):
            with patch.object(mod, "detect_default_branch", return_value="main"):
                branch, err, pre_merged = mod.find_task_branch(9)

        assert branch is None
        assert "No branch found" in err
        assert pre_merged is False
