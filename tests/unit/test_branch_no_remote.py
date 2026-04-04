"""Unit tests for tusk-branch.py graceful handling of missing git remote (Issue #444).

When no git remote 'origin' is configured, tusk branch should skip the pull
step and create the branch locally, printing a warning instead of hard-failing.
"""

import importlib.util
import os
import subprocess
import sys
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


class TestBranchNoRemote:
    """tusk branch succeeds when no git remote is configured."""

    def _make_run(self, has_remote=True):
        """Return a fake run() that simulates presence/absence of origin remote."""

        def fake_run(args, check=True):
            # _has_remote check
            if args[:3] == ["git", "remote", "get-url"]:
                if has_remote:
                    return _cp(0, stdout="https://github.com/test/repo.git\n")
                return _cp(2, stderr="fatal: No such remote 'origin'")
            # detect default branch — remote HEAD unavailable when no remote
            if args[:4] == ["git", "remote", "set-head", "origin"]:
                if has_remote:
                    return _cp(0)
                return _cp(2, stderr="error: could not fetch")
            if args[:2] == ["git", "symbolic-ref"]:
                if has_remote:
                    return _cp(0, stdout="refs/remotes/origin/main\n")
                return _cp(1)  # no remote HEAD
            # gh fallback for default branch
            if args[:2] == ["gh", "repo"]:
                return _cp(1)  # gh not available / no remote
            # dirty check
            if args[:2] == ["git", "status"]:
                return _cp(0, stdout="")
            # checkout default
            if args == ["git", "checkout", "main"]:
                return _cp(0)
            # pull — should not be called when no remote
            if args[:3] == ["git", "pull"]:
                if has_remote:
                    return _cp(0)
                return _cp(128, stderr="fatal: 'origin' does not appear to be a git repository")
            # list existing branches
            if args[:3] == ["git", "branch", "--list"]:
                return _cp(0, stdout="")
            # create branch
            if args[:2] == ["git", "checkout"] and "-b" in args:
                return _cp(0)
            return _cp(0)

        return fake_run

    def test_no_remote_succeeds(self, capsys):
        """tusk branch exits 0 when no remote is configured."""
        mod = _load_module()
        fake_run = self._make_run(has_remote=False)

        with patch.object(mod, "run", side_effect=fake_run):
            rc = mod.main([".", "1", "test-slug"])

        assert rc == 0
        out, err = capsys.readouterr()
        assert "feature/TASK-1-test-slug" in out

    def test_no_remote_prints_warning(self, capsys):
        """A warning is printed when pull is skipped."""
        mod = _load_module()
        fake_run = self._make_run(has_remote=False)

        with patch.object(mod, "run", side_effect=fake_run):
            mod.main([".", "1", "test-slug"])

        _, err = capsys.readouterr()
        assert "no git remote" in err
        assert "skipping pull" in err

    def test_no_remote_does_not_call_pull(self):
        """git pull is never invoked when no remote exists."""
        mod = _load_module()
        pull_calls = []

        def fake_run(args, check=True):
            if args[:3] == ["git", "remote", "get-url"]:
                return _cp(2, stderr="fatal: No such remote 'origin'")
            if args[:4] == ["git", "remote", "set-head", "origin"]:
                return _cp(2)
            if args[:2] == ["git", "symbolic-ref"]:
                return _cp(1)
            if args[:2] == ["gh", "repo"]:
                return _cp(1)
            if args[:2] == ["git", "status"]:
                return _cp(0, stdout="")
            if args == ["git", "checkout", "main"]:
                return _cp(0)
            if args[:3] == ["git", "pull"]:
                pull_calls.append(args)
                return _cp(128)
            if args[:3] == ["git", "branch", "--list"]:
                return _cp(0, stdout="")
            if args[:2] == ["git", "checkout"] and "-b" in args:
                return _cp(0)
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            mod.main([".", "1", "test-slug"])

        assert pull_calls == [], f"git pull should not be called when no remote: {pull_calls}"

    def test_with_remote_still_pulls(self, capsys):
        """Normal behavior: git pull is called when remote exists."""
        mod = _load_module()
        pull_called = []

        def fake_run(args, check=True):
            if args[:3] == ["git", "remote", "get-url"]:
                return _cp(0, stdout="https://github.com/test/repo.git\n")
            if args[:4] == ["git", "remote", "set-head", "origin"]:
                return _cp(0)
            if args[:2] == ["git", "symbolic-ref"]:
                return _cp(0, stdout="refs/remotes/origin/main\n")
            if args[:2] == ["git", "status"]:
                return _cp(0, stdout="")
            if args == ["git", "checkout", "main"]:
                return _cp(0)
            if args[:2] == ["git", "pull"]:
                pull_called.append(args)
                return _cp(0)
            if args[:3] == ["git", "branch", "--list"]:
                return _cp(0, stdout="")
            if args[:2] == ["git", "checkout"] and "-b" in args:
                return _cp(0)
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            rc = mod.main([".", "1", "test-slug"])

        assert rc == 0
        assert len(pull_called) == 1, "git pull should be called when remote exists"
