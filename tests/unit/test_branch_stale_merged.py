"""Unit tests for stale-merged-branch detection in tusk-branch.py (Issue #437).

When an existing feature branch for a task ID has its tip already merged into
the default branch, tusk branch should warn the user and either:
  - interactive: prompt y/N, delete + recreate on "y", abort on anything else
  - non-interactive: abort without switching to the stale branch
"""

import importlib.util
import os
import subprocess
import sys
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BRANCH_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-branch.py")


def _load_module():
    tusk_loader_mock = MagicMock()
    db_lib_mock = MagicMock()
    db_lib_mock.checkpoint_wal = MagicMock()
    tusk_loader_mock.load.return_value = db_lib_mock
    with patch.dict("sys.modules", {"tusk_loader": tusk_loader_mock}):
        spec = importlib.util.spec_from_file_location("tusk_branch", BRANCH_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _cp(returncode, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestStaleMergedBranchDetection:
    """tusk branch detects existing branches already merged into default."""

    def _make_run(self, task_id, existing_branch, is_merged, delete_ok=True, create_ok=True):
        """Return a fake run() that simulates the stale-merged scenario."""

        def fake_run(args, check=True):
            cmd = args[:3]
            # detect default branch
            if args[:3] == ["git", "remote", "set-head", "origin", "--auto"]:
                return _cp(0)
            if args[:2] == ["git", "symbolic-ref"]:
                return _cp(0, stdout="refs/remotes/origin/main\n")
            # dirty check
            if args[:2] == ["git", "status"]:
                return _cp(0, stdout="")
            # checkout default
            if args == ["git", "checkout", "main"]:
                return _cp(0)
            # pull
            if args[:3] == ["git", "pull"]:
                return _cp(0)
            # list existing branches
            if args[:3] == ["git", "branch", "--list"]:
                return _cp(0, stdout=f"  {existing_branch}\n")
            # rev-parse
            if args[:2] == ["git", "rev-parse"]:
                return _cp(0, stdout="abc123\n")
            # merge-base --is-ancestor
            if args[:2] == ["git", "merge-base"]:
                return _cp(0 if is_merged else 1)
            # delete branch
            if args[:3] == ["git", "branch", "-D"]:
                return _cp(0 if delete_ok else 1, stderr="" if delete_ok else "cannot delete")
            # create branch
            if args[:2] == ["git", "checkout"] and "-b" in args:
                return _cp(0 if create_ok else 1, stderr="" if create_ok else "already exists")
            # switch to existing (non-merged path)
            if args[:2] == ["git", "checkout"]:
                return _cp(0)
            return _cp(0)

        return fake_run

    def test_non_interactive_aborts_when_merged(self, capsys):
        mod = _load_module()
        fake_run = self._make_run(999, "feature/TASK-999-old", is_merged=True)

        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod.sys.stdin, "isatty", return_value=False):
            rc = mod.main([".", "999", "new-slug"])

        assert rc == 2
        captured = capsys.readouterr()
        assert "already merged" in captured.err
        assert "Aborting" in captured.err

    def test_non_interactive_does_not_checkout_stale_branch(self, capsys):
        mod = _load_module()
        checked_out = []

        def fake_run(args, check=True):
            if args[:2] == ["git", "remote"]:
                return _cp(0)
            if args[:2] == ["git", "symbolic-ref"]:
                return _cp(0, stdout="refs/remotes/origin/main\n")
            if args[:2] == ["git", "status"]:
                return _cp(0, stdout="")
            if args == ["git", "checkout", "main"]:
                return _cp(0)
            if args[:3] == ["git", "pull"]:
                return _cp(0)
            if args[:3] == ["git", "branch", "--list"]:
                return _cp(0, stdout="  feature/TASK-999-old\n")
            if args[:2] == ["git", "rev-parse"]:
                return _cp(0, stdout="abc123\n")
            if args[:3] == ["git", "merge-base"]:
                return _cp(0)  # is-ancestor → merged
            if args[:2] == ["git", "checkout"]:
                checked_out.append(args)
                return _cp(0)
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod.sys.stdin, "isatty", return_value=False):
            mod.main([".", "999", "new-slug"])

        # Only "git checkout main" should have been called, never the stale branch
        stale_checkouts = [a for a in checked_out if "feature/TASK-999-old" in a]
        assert stale_checkouts == [], f"Should not have checked out stale branch: {stale_checkouts}"

    def test_interactive_yes_deletes_and_recreates(self, capsys):
        mod = _load_module()
        fake_run = self._make_run(999, "feature/TASK-999-old", is_merged=True)

        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod.sys.stdin, "isatty", return_value=True), \
             patch.object(mod.sys.stdin, "readline", return_value="y\n"):
            rc = mod.main([".", "999", "new-slug"])

        assert rc == 0
        out, err = capsys.readouterr()
        assert "feature/TASK-999-new-slug" in out
        assert "already merged" in err

    def test_interactive_no_aborts(self, capsys):
        mod = _load_module()
        fake_run = self._make_run(999, "feature/TASK-999-old", is_merged=True)

        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod.sys.stdin, "isatty", return_value=True), \
             patch.object(mod.sys.stdin, "readline", return_value="n\n"):
            rc = mod.main([".", "999", "new-slug"])

        assert rc == 2
        captured = capsys.readouterr()
        assert "Aborting" in captured.err

    def test_interactive_empty_answer_aborts(self, capsys):
        mod = _load_module()
        fake_run = self._make_run(999, "feature/TASK-999-old", is_merged=True)

        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod.sys.stdin, "isatty", return_value=True), \
             patch.object(mod.sys.stdin, "readline", return_value="\n"):
            rc = mod.main([".", "999", "new-slug"])

        assert rc == 2

    def test_not_merged_uses_existing_behavior(self, capsys):
        """When branch exists but is NOT merged, the old warn-and-switch behavior applies."""
        mod = _load_module()
        fake_run = self._make_run(999, "feature/TASK-999-old", is_merged=False)

        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod.sys.stdin, "isatty", return_value=False):
            rc = mod.main([".", "999", "new-slug"])

        assert rc == 0
        captured = capsys.readouterr()
        assert "already exists" in captured.err
        assert "Switching to it" in captured.err

    def test_uses_merge_base_is_ancestor(self, capsys):
        """Verify git merge-base --is-ancestor is called with branch tip and default branch."""
        mod = _load_module()
        merge_base_calls = []

        def fake_run(args, check=True):
            if args[:2] == ["git", "remote"]:
                return _cp(0)
            if args[:2] == ["git", "symbolic-ref"]:
                return _cp(0, stdout="refs/remotes/origin/main\n")
            if args[:2] == ["git", "status"]:
                return _cp(0, stdout="")
            if args == ["git", "checkout", "main"]:
                return _cp(0)
            if args[:3] == ["git", "pull"]:
                return _cp(0)
            if args[:3] == ["git", "branch", "--list"]:
                return _cp(0, stdout="  feature/TASK-42-old\n")
            if args[:2] == ["git", "rev-parse"]:
                return _cp(0, stdout="deadbeef\n")
            if args[:2] == ["git", "merge-base"]:
                merge_base_calls.append(args)
                return _cp(1)  # not merged → old behavior
            if args[:2] == ["git", "checkout"]:
                return _cp(0)
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod.sys.stdin, "isatty", return_value=False):
            mod.main([".", "42", "new-slug"])

        assert len(merge_base_calls) == 1
        assert merge_base_calls[0] == [
            "git", "merge-base", "--is-ancestor", "deadbeef", "main"
        ]
