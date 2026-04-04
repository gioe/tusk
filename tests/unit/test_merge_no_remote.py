"""Unit tests for tusk-merge.py graceful handling of missing git remote (Issue #444).

When no git remote 'origin' is configured, tusk merge should skip the pull
and push steps and proceed with local-only merge, printing warnings instead
of hard-failing.
"""

import importlib.util
import json
import os
import sqlite3
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


class TestHasRemote:
    """Unit tests for _has_remote helper."""

    def test_returns_true_when_remote_exists(self):
        mod = _load_module()
        with patch.object(mod, "run", return_value=_cp(0, stdout="https://github.com/test/repo.git")):
            assert mod._has_remote() is True

    def test_returns_false_when_no_remote(self):
        mod = _load_module()
        with patch.object(mod, "run", return_value=_cp(2, stderr="fatal: No such remote 'origin'")):
            assert mod._has_remote() is False

    def test_checks_named_remote(self):
        mod = _load_module()
        calls = []

        def fake_run(args, check=True):
            calls.append(args)
            return _cp(0, stdout="url")

        with patch.object(mod, "run", side_effect=fake_run):
            mod._has_remote("upstream")

        assert calls[0] == ["git", "remote", "get-url", "upstream"]


class TestMergeNoRemote:
    """tusk merge skips pull/push when no remote is configured."""

    def _make_run(self, has_remote=False, task_id=1):
        """Return a fake run() for the merge main() flow without a remote."""
        task_done_json = json.dumps({
            "task_id": task_id,
            "summary": "test",
            "unblocked_tasks": [],
        })
        pull_calls = []
        push_calls = []

        def fake_run(args, check=True):
            cmd_str = " ".join(args[:3])
            # _has_remote
            if args[:3] == ["git", "remote", "get-url"]:
                if has_remote:
                    return _cp(0, stdout="https://github.com/test/repo.git")
                return _cp(2, stderr="fatal: No such remote 'origin'")
            # detect default branch
            if args[:4] == ["git", "remote", "set-head", "origin"]:
                return _cp(0 if has_remote else 2)
            if args[:2] == ["git", "symbolic-ref"]:
                if has_remote:
                    return _cp(0, stdout="refs/remotes/origin/main\n")
                return _cp(1)
            if args[:2] == ["gh", "repo"]:
                return _cp(1)
            # find_task_branch
            if args[:3] == ["git", "branch", "--list"]:
                return _cp(0, stdout=f"  feature/TASK-{task_id}-test\n")
            # dirty check
            if args[:2] == ["git", "diff"]:
                return _cp(0, stdout="")
            # session-close
            if len(args) >= 2 and "session-close" in str(args):
                return _cp(0)
            # checkout default
            if args == ["git", "checkout", "main"]:
                return _cp(0)
            # pull
            if "pull" in args:
                pull_calls.append(args)
                if has_remote:
                    return _cp(0)
                return _cp(128, stderr="fatal: 'origin' does not appear to be a git repository")
            # git log for cherry/diverge checks
            if args[:2] == ["git", "log"]:
                return _cp(0, stdout=f"abc123 [TASK-{task_id}] test\n")
            # git cherry
            if args[:2] == ["git", "cherry"]:
                return _cp(0, stdout=f"+ abc123\n")
            # merge --ff-only
            if args[:2] == ["git", "merge"]:
                return _cp(0)
            # push
            if "push" in args:
                push_calls.append(args)
                if has_remote:
                    return _cp(0)
                return _cp(128, stderr="fatal: 'origin' does not appear to be a git repository")
            # branch delete
            if args[:2] == ["git", "branch"] and ("-d" in args or "-D" in args):
                return _cp(0)
            # task-done
            if "task-done" in str(args):
                return _cp(0, stdout=task_done_json)
            # stash list (for _try_pop_stash)
            if args[:2] == ["git", "stash"]:
                return _cp(0, stdout="")
            return _cp(0)

        return fake_run, pull_calls, push_calls

    def _make_conn_mock(self, task_id=1, session_id=1):
        """Create a mock DB connection that returns a valid open session."""
        conn = MagicMock()
        cursor = MagicMock()
        # First call: open sessions query
        # Second call: might be for validation
        conn.execute.side_effect = [
            # _autodetect_session: open sessions
            MagicMock(fetchall=MagicMock(return_value=[(session_id, "2026-01-01")])),
        ]
        return conn

    def test_no_remote_skips_pull(self, capsys, tmp_path):
        """git pull is not called when no remote exists."""
        mod = _load_module()
        fake_run, pull_calls, push_calls = self._make_run(has_remote=False)

        conn_mock = self._make_conn_mock()
        mod._db_lib = MagicMock()
        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod, "get_connection", return_value=conn_mock), \
             patch("os.path.exists", return_value=False), \
             patch("os.rename"):
            rc = mod.main([str(tmp_path / "tasks.db"), str(tmp_path / "config.json"),
                           "1", "--session", "1"])

        assert pull_calls == [], f"git pull should not be called: {pull_calls}"
        _, err = capsys.readouterr()
        assert "skipping pull" in err

    def test_no_remote_skips_push(self, capsys, tmp_path):
        """git push is not called when no remote exists."""
        mod = _load_module()
        fake_run, pull_calls, push_calls = self._make_run(has_remote=False)

        conn_mock = self._make_conn_mock()
        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod, "get_connection", return_value=conn_mock), \
             patch("os.path.exists", return_value=False), \
             patch("os.rename"):
            rc = mod.main([str(tmp_path / "tasks.db"), str(tmp_path / "config.json"),
                           "1", "--session", "1"])

        assert push_calls == [], f"git push should not be called: {push_calls}"
        _, err = capsys.readouterr()
        assert "skipping push" in err

    def test_no_remote_exits_zero(self, capsys, tmp_path):
        """tusk merge exits 0 when no remote is configured."""
        mod = _load_module()
        fake_run, _, _ = self._make_run(has_remote=False)

        conn_mock = self._make_conn_mock()
        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod, "get_connection", return_value=conn_mock), \
             patch("os.path.exists", return_value=False), \
             patch("os.rename"):
            rc = mod.main([str(tmp_path / "tasks.db"), str(tmp_path / "config.json"),
                           "1", "--session", "1"])

        assert rc == 0

    def test_with_remote_calls_pull_and_push(self, capsys, tmp_path):
        """Normal behavior: git pull and push are called when remote exists."""
        mod = _load_module()
        fake_run, pull_calls, push_calls = self._make_run(has_remote=True)

        conn_mock = self._make_conn_mock()
        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod, "get_connection", return_value=conn_mock), \
             patch("os.path.exists", return_value=False), \
             patch("os.rename"):
            rc = mod.main([str(tmp_path / "tasks.db"), str(tmp_path / "config.json"),
                           "1", "--session", "1"])

        assert rc == 0
        assert len(pull_calls) == 1, "git pull should be called when remote exists"
        assert len(push_calls) == 1, "git push should be called when remote exists"
