"""Unit tests for _warn_branch_auto_stash (Issue #727).

After a successful tusk merge/abandon, preserve any leftover
``tusk-branch: auto-stash for TASK-<id>`` entry that was created by an earlier
``tusk branch <id>`` invocation when the working tree was dirty, and warn the
user how to restore or drop it manually. This module covers the warning helper
in isolation.
"""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MERGE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-merge.py")
GIT_HELPERS_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-git-helpers.py")


def _load_git_helpers():
    spec = importlib.util.spec_from_file_location("tusk_git_helpers", GIT_HELPERS_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_module():
    tusk_loader_mock = MagicMock()
    db_lib_mock = MagicMock()
    db_lib_mock.get_connection = MagicMock()
    db_lib_mock.checkpoint_wal = MagicMock()

    def load(name):
        if name == "tusk-git-helpers":
            return _load_git_helpers()
        return db_lib_mock

    tusk_loader_mock.load.side_effect = load
    with patch.dict("sys.modules", {"tusk_loader": tusk_loader_mock}):
        spec = importlib.util.spec_from_file_location("tusk_merge", MERGE_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _cp(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestWarnBranchAutoStash:
    def test_warns_and_preserves_matching_entry(self, capsys):
        mod = _load_module()
        calls: list[list[str]] = []

        def fake_run(args, check=True):
            calls.append(args)
            if args[:3] == ["git", "stash", "list"]:
                return _cp(
                    0,
                    stdout=(
                        "stash@{0}: On main: tusk-branch: auto-stash for TASK-42\n"
                        "stash@{1}: On main: unrelated work\n"
                    ),
                )
            if args[:3] == ["git", "stash", "drop"]:
                return _cp(0)
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._warn_branch_auto_stash(42)

        assert ["git", "stash", "drop", "stash@{0}"] not in calls
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Warning: preserved tusk branch auto-stash for TASK-42" in captured.err
        assert "git stash pop stash@{0}" in captured.err
        assert "git stash drop stash@{0}" in captured.err

    def test_warns_about_correct_index_when_match_is_not_top(self, capsys):
        mod = _load_module()
        calls: list[list[str]] = []

        def fake_run(args, check=True):
            calls.append(args)
            if args[:3] == ["git", "stash", "list"]:
                return _cp(
                    0,
                    stdout=(
                        "stash@{0}: On main: unrelated work\n"
                        "stash@{1}: On main: tusk-merge: auto-stash for TASK-42\n"
                        "stash@{2}: On main: tusk-branch: auto-stash for TASK-42\n"
                    ),
                )
            if args[:3] == ["git", "stash", "drop"]:
                return _cp(0)
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._warn_branch_auto_stash(42)

        # Preserve the branch-stash at index 2, not the merge-stash at index 1
        # or the unrelated entry at index 0.
        drop_calls = [c for c in calls if c[:3] == ["git", "stash", "drop"]]
        assert drop_calls == []
        captured = capsys.readouterr()
        assert "git stash pop stash@{2}" in captured.err

    def test_silent_when_no_entry_found(self, capsys):
        mod = _load_module()
        calls: list[list[str]] = []

        def fake_run(args, check=True):
            calls.append(args)
            if args[:3] == ["git", "stash", "list"]:
                return _cp(0, stdout="stash@{0}: On main: unrelated\n")
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._warn_branch_auto_stash(42)

        # No drop was attempted.
        assert not any(c[:3] == ["git", "stash", "drop"] for c in calls)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_silent_when_stash_list_empty(self, capsys):
        mod = _load_module()
        calls: list[list[str]] = []

        def fake_run(args, check=True):
            calls.append(args)
            if args[:3] == ["git", "stash", "list"]:
                return _cp(0, stdout="")
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._warn_branch_auto_stash(42)

        assert not any(c[:3] == ["git", "stash", "drop"] for c in calls)
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_silent_when_stash_list_fails(self, capsys):
        mod = _load_module()
        calls: list[list[str]] = []

        def fake_run(args, check=True):
            calls.append(args)
            if args[:3] == ["git", "stash", "list"]:
                return _cp(1, stderr="fatal: not a git repository")
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._warn_branch_auto_stash(42)

        assert not any(c[:3] == ["git", "stash", "drop"] for c in calls)
        captured = capsys.readouterr()
        # Failure is silent — no warning, no error.
        assert captured.err == ""

    def test_does_not_match_task_id_prefix_collision(self, capsys):
        """TASK-2 must not match a TASK-29 line (substring would; endswith does not)."""
        mod = _load_module()
        calls: list[list[str]] = []

        def fake_run(args, check=True):
            calls.append(args)
            if args[:3] == ["git", "stash", "list"]:
                return _cp(
                    0,
                    stdout="stash@{0}: On main: tusk-branch: auto-stash for TASK-29\n",
                )
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._warn_branch_auto_stash(2)

        # No drop — TASK-29 is not TASK-2.
        assert not any(c[:3] == ["git", "stash", "drop"] for c in calls)

    def test_does_not_match_merge_prefix(self, capsys):
        """``tusk-merge:`` prefix entries are owned by _try_pop_stash, not this function."""
        mod = _load_module()
        calls: list[list[str]] = []

        def fake_run(args, check=True):
            calls.append(args)
            if args[:3] == ["git", "stash", "list"]:
                return _cp(
                    0,
                    stdout="stash@{0}: On main: tusk-merge: auto-stash for TASK-42\n",
                )
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._warn_branch_auto_stash(42)

        assert not any(c[:3] == ["git", "stash", "drop"] for c in calls)
