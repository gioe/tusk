"""Unit tests for _try_pop_stash auto-resolve of generated lockfile conflicts (Issue #393).

When git stash pop fails due to conflicts in known generated lockfiles
(Package.resolved, package-lock.json, etc.), tusk-merge.py should auto-resolve
by checking out the stash version and dropping the stash entry instead of leaving
the user with a stranded stash.
"""

import importlib.util
import os
import subprocess
import sys
from unittest.mock import MagicMock, call, patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MERGE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-merge.py")


def _load_module():
    # Patch tusk_loader so we don't need the full install environment.
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


class TestTryPopStash:
    """Tests for _try_pop_stash conflict handling."""

    def _stash_list_output(self, index: int, task_id: int) -> str:
        return f"stash@{{{index}}}: On branch feature/TASK-{task_id}-foo: tusk-merge: auto-stash for TASK-{task_id}\n"

    def test_successful_pop_prints_note(self, capsys):
        mod = _load_module()

        def fake_run(args, check=True):
            if args[:2] == ["git", "stash"] and args[2] == "list":
                return _cp(0, stdout=self._stash_list_output(0, 42))
            if args[:2] == ["git", "stash"] and args[2] == "pop":
                return _cp(0)
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._try_pop_stash(42)

        captured = capsys.readouterr()
        assert "stash restored to working tree" in captured.err

    def test_auto_resolves_package_resolved_conflict(self, capsys):
        mod = _load_module()
        calls = []

        def fake_run(args, check=True):
            calls.append(args)
            if args[:2] == ["git", "stash"] and args[2] == "list":
                return _cp(0, stdout=self._stash_list_output(0, 99))
            if args[:2] == ["git", "stash"] and args[2] == "pop":
                return _cp(1, stderr="CONFLICT (content): Merge conflict in Package.resolved")
            if args[:2] == ["git", "diff"]:
                return _cp(0, stdout="Package.resolved\n")
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._try_pop_stash(99)

        captured = capsys.readouterr()
        assert "auto-resolved" in captured.err
        assert "Package.resolved" in captured.err
        assert "Stash restored" in captured.err
        # Must have checked out stash version and added the file
        assert any(
            a[:3] == ["git", "checkout", "stash@{0}"] and "Package.resolved" in a
            for a in calls
        )
        assert any(a[:2] == ["git", "add"] and "Package.resolved" in a for a in calls)
        # Must have dropped the stash entry
        assert any(a[:3] == ["git", "stash", "drop"] for a in calls)

    def test_auto_resolves_multiple_lockfiles(self, capsys):
        mod = _load_module()
        calls = []

        def fake_run(args, check=True):
            calls.append(args)
            if args[:2] == ["git", "stash"] and args[2] == "list":
                return _cp(0, stdout=self._stash_list_output(2, 7))
            if args[:2] == ["git", "stash"] and args[2] == "pop":
                return _cp(1, stderr="CONFLICT in package-lock.json\nCONFLICT in yarn.lock")
            if args[:2] == ["git", "diff"]:
                return _cp(0, stdout="package-lock.json\nyarn.lock\n")
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._try_pop_stash(7)

        captured = capsys.readouterr()
        assert "auto-resolved" in captured.err
        assert "package-lock.json" in captured.err
        assert "yarn.lock" in captured.err

    def test_does_not_auto_resolve_non_generated_conflict(self, capsys):
        mod = _load_module()

        def fake_run(args, check=True):
            if args[:2] == ["git", "stash"] and args[2] == "list":
                return _cp(0, stdout=self._stash_list_output(0, 5))
            if args[:2] == ["git", "stash"] and args[2] == "pop":
                return _cp(1, stderr="CONFLICT in src/main.py")
            if args[:2] == ["git", "diff"]:
                return _cp(0, stdout="src/main.py\n")
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._try_pop_stash(5)

        captured = capsys.readouterr()
        assert "auto-resolved" not in captured.err
        assert "run 'git stash list'" in captured.err

    def test_does_not_auto_resolve_mixed_conflict(self, capsys):
        """When both generated and non-generated files conflict, fall back to manual."""
        mod = _load_module()

        def fake_run(args, check=True):
            if args[:2] == ["git", "stash"] and args[2] == "list":
                return _cp(0, stdout=self._stash_list_output(0, 10))
            if args[:2] == ["git", "stash"] and args[2] == "pop":
                return _cp(1)
            if args[:2] == ["git", "diff"]:
                return _cp(0, stdout="Package.resolved\nsrc/config.py\n")
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._try_pop_stash(10)

        captured = capsys.readouterr()
        assert "auto-resolved" not in captured.err
        assert "run 'git stash list'" in captured.err

    def test_warns_when_stash_entry_not_found(self, capsys):
        mod = _load_module()

        def fake_run(args, check=True):
            if args[:2] == ["git", "stash"] and args[2] == "list":
                return _cp(0, stdout="stash@{0}: WIP on main: unrelated\n")
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod._try_pop_stash(55)

        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "could not find" in captured.err
