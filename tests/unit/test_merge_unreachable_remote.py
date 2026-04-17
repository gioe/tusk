"""Unit tests for tusk-merge.py graceful handling of an unreachable origin (Issue #470).

When `origin` exists but is unreachable (DNS failure, connection refused, 404,
dead host), `git pull` exits non-zero. tusk merge should detect the
network-level failure and fall back to merging from local state instead of
hard-failing — mirroring the existing no-remote path and the equivalent
fallback already in tusk-branch.py (Issue #473 / TASK-99).
"""

import importlib.util
import json
import os
import subprocess
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MERGE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-merge.py")
GIT_HELPERS_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-git-helpers.py")


def _load_real_git_helpers():
    spec = importlib.util.spec_from_file_location("tusk_git_helpers", GIT_HELPERS_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_module():
    tusk_loader_mock = MagicMock()
    db_lib_mock = MagicMock()
    db_lib_mock.get_connection = MagicMock()
    db_lib_mock.checkpoint_wal = MagicMock()
    real_git_helpers = _load_real_git_helpers()

    def _load(name):
        if name == "tusk-git-helpers":
            return real_git_helpers
        return db_lib_mock

    tusk_loader_mock.load.side_effect = _load
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


def _make_run(pull_rc: int, pull_stderr: str, task_id: int = 1):
    """Build a fake subprocess.run that simulates a reachable remote (origin
    exists) but lets the caller control the outcome of `git pull`. The push
    step is treated as successful so that we isolate pull-step behavior."""
    task_done_json = json.dumps({"task_id": task_id, "summary": "test", "unblocked_tasks": []})
    pull_calls: list[list[str]] = []
    push_calls: list[list[str]] = []
    checkout_calls: list[list[str]] = []

    def fake_run(args, check=True):
        if args[:3] == ["git", "remote", "get-url"]:
            return _cp(0, stdout="https://example.com/nonexistent.git\n")
        if args[:4] == ["git", "remote", "set-head", "origin"]:
            return _cp(0)
        if args[:2] == ["git", "symbolic-ref"]:
            return _cp(0, stdout="refs/remotes/origin/main\n")
        if args[:3] == ["git", "branch", "--list"]:
            return _cp(0, stdout=f"  feature/TASK-{task_id}-test\n")
        if args[:2] == ["git", "diff"]:
            return _cp(0, stdout="")
        if args[:3] == ["git", "stash", "list"]:
            return _cp(0, stdout="")
        if args[:2] == ["git", "stash"]:
            return _cp(0)
        if args == ["git", "checkout", "main"]:
            checkout_calls.append(args)
            return _cp(0)
        if args[:2] == ["git", "checkout"]:
            checkout_calls.append(args)
            return _cp(0)
        if "pull" in args:
            pull_calls.append(args)
            return _cp(pull_rc, stderr=pull_stderr)
        if args[:2] == ["git", "log"]:
            return _cp(0, stdout=f"abc123 [TASK-{task_id}] test\n")
        if args[:2] == ["git", "cherry"]:
            return _cp(0, stdout="+ abc123\n")
        if args[:2] == ["git", "merge"]:
            return _cp(0)
        if "push" in args:
            push_calls.append(args)
            return _cp(0)
        if args[:2] == ["git", "branch"] and ("-d" in args or "-D" in args):
            return _cp(0)
        if "task-done" in str(args):
            return _cp(0, stdout=task_done_json)
        if "session-close" in str(args):
            return _cp(0)
        return _cp(0)

    return fake_run, pull_calls, push_calls, checkout_calls


def _make_conn_mock(session_id=1):
    conn = MagicMock()
    conn.execute.side_effect = [
        MagicMock(fetchall=MagicMock(return_value=[(session_id, "2026-01-01")])),
    ]
    return conn


class TestMergeUnreachableRemote:
    """tusk merge falls back to local state when origin is unreachable."""

    def _run_merge(self, mod, fake_run, tmp_path):
        conn_mock = _make_conn_mock()
        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod, "get_connection", return_value=conn_mock), \
             patch("os.path.exists", return_value=False), \
             patch("os.rename"):
            return mod.main([str(tmp_path / "tasks.db"), str(tmp_path / "config.json"),
                             "1", "--session", "1"])

    def test_dns_failure_succeeds(self, capsys, tmp_path):
        mod = _load_module()
        fake_run, _, _, _ = _make_run(
            pull_rc=128,
            pull_stderr=(
                "fatal: unable to access 'https://example.com/nonexistent.git/': "
                "Could not resolve host: example.com"
            ),
        )

        rc = self._run_merge(mod, fake_run, tmp_path)

        assert rc == 0
        _, err = capsys.readouterr()
        assert "could not reach origin" in err
        assert "skipping pull" in err

    def test_repo_not_found_succeeds(self, capsys, tmp_path):
        mod = _load_module()
        fake_run, _, _, _ = _make_run(
            pull_rc=128,
            pull_stderr=(
                "remote: Repository not found.\n"
                "fatal: repository 'https://github.com/nobody/nothing.git/' not found"
            ),
        )

        rc = self._run_merge(mod, fake_run, tmp_path)

        assert rc == 0
        _, err = capsys.readouterr()
        assert "could not reach origin" in err

    def test_connection_refused_succeeds(self, capsys, tmp_path):
        mod = _load_module()
        fake_run, _, _, _ = _make_run(
            pull_rc=128,
            pull_stderr="fatal: unable to access '...': Failed to connect to ...: Connection refused",
        )

        rc = self._run_merge(mod, fake_run, tmp_path)

        assert rc == 0

    def test_merge_conflict_still_fails(self, capsys, tmp_path):
        """Non-network pull failures (merge conflicts, divergent histories) must
        still exit 2 — the fallback is network-specific."""
        mod = _load_module()
        fake_run, _, _, checkout_calls = _make_run(
            pull_rc=1,
            pull_stderr=(
                "CONFLICT (content): Merge conflict in foo.py\n"
                "Automatic merge failed; fix conflicts and then commit the result."
            ),
        )

        rc = self._run_merge(mod, fake_run, tmp_path)

        assert rc == 2
        _, err = capsys.readouterr()
        assert "git pull failed" in err
        # On hard pull failure the script restores the feature branch so the
        # user can investigate without losing their place.
        assert any("feature/TASK-1-test" in args for args in checkout_calls)


class TestIsRemoteUnreachable:
    """Unit tests for the _is_remote_unreachable heuristic ported into tusk-merge.py."""

    def test_matches_common_network_errors(self):
        mod = _load_module()
        cases = [
            "fatal: unable to access 'https://x/': The requested URL returned error: 404",
            "fatal: Could not resolve host: example.com",
            "fatal: Could not read from remote repository.",
            "Connection refused",
            "Connection timed out",
            "fatal: repository 'https://x' not found",
            "ssh: connect to host x port 22: Network is unreachable",
            "ssh: Could not resolve hostname x: Temporary failure in name resolution",
        ]
        for stderr in cases:
            assert mod._is_remote_unreachable(stderr), f"should match: {stderr!r}"

    def test_does_not_match_merge_conflict(self):
        mod = _load_module()
        stderr = (
            "CONFLICT (content): Merge conflict in foo.py\n"
            "Automatic merge failed; fix conflicts and then commit the result."
        )
        assert not mod._is_remote_unreachable(stderr)

    def test_does_not_match_non_fast_forward(self):
        mod = _load_module()
        stderr = (
            "hint: Updates were rejected because the tip of your current branch "
            "is behind its remote counterpart."
        )
        assert not mod._is_remote_unreachable(stderr)
