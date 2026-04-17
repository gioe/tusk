"""Unit tests for tusk-branch.py graceful handling of an unreachable origin (Issue #473).

When `origin` exists but is unreachable (DNS failure, connection refused, 404,
dead host), `git pull` exits non-zero. tusk branch should detect the
network-level failure and fall back to branching from local HEAD instead of
hard-failing, mirroring the existing no-remote path.
"""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock, patch

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


def _fake_run_with_pull(pull_rc: int, pull_stderr: str, dirty: bool = False):
    """Build a fake subprocess.run that simulates a reachable remote (origin
    exists) but lets the caller control the outcome of `git pull`."""
    stash_pops: list[list[str]] = []

    status_stdout = " M some_file.py\n" if dirty else ""

    def fake_run(args, check=True):
        if args[:3] == ["git", "remote", "get-url"]:
            return _cp(0, stdout="https://example.com/nonexistent.git\n")
        if args[:4] == ["git", "remote", "set-head", "origin"]:
            return _cp(0)
        if args[:2] == ["git", "symbolic-ref"]:
            return _cp(0, stdout="refs/remotes/origin/main\n")
        if args[:2] == ["git", "status"]:
            return _cp(0, stdout=status_stdout)
        if args[:3] == ["git", "stash", "push"]:
            return _cp(0)
        if args[:3] == ["git", "stash", "pop"]:
            stash_pops.append(args)
            return _cp(0)
        if args == ["git", "checkout", "main"]:
            return _cp(0)
        if args[:2] == ["git", "pull"]:
            return _cp(pull_rc, stderr=pull_stderr)
        if args[:3] == ["git", "branch", "--list"]:
            return _cp(0, stdout="")
        if args[:2] == ["git", "checkout"] and "-b" in args:
            return _cp(0)
        return _cp(0)

    return fake_run, stash_pops


class TestBranchUnreachableRemote:
    """tusk branch falls back to local HEAD when origin is unreachable."""

    def test_dns_failure_succeeds(self, capsys):
        mod = _load_module()
        fake_run, _ = _fake_run_with_pull(
            pull_rc=128,
            pull_stderr=(
                "fatal: unable to access 'https://example.com/nonexistent.git/': "
                "Could not resolve host: example.com"
            ),
        )
        with patch.object(mod, "run", side_effect=fake_run):
            rc = mod.main([".", "1", "test-slug"])

        assert rc == 0
        out, err = capsys.readouterr()
        assert "feature/TASK-1-test-slug" in out
        assert "could not reach origin" in err
        assert "skipping pull" in err

    def test_repo_not_found_succeeds(self, capsys):
        mod = _load_module()
        fake_run, _ = _fake_run_with_pull(
            pull_rc=128,
            pull_stderr=(
                "remote: Repository not found.\n"
                "fatal: repository 'https://github.com/nobody/nothing.git/' not found"
            ),
        )
        with patch.object(mod, "run", side_effect=fake_run):
            rc = mod.main([".", "2", "test-slug"])

        assert rc == 0
        _, err = capsys.readouterr()
        assert "could not reach origin" in err

    def test_connection_refused_succeeds(self, capsys):
        mod = _load_module()
        fake_run, _ = _fake_run_with_pull(
            pull_rc=128,
            pull_stderr="fatal: unable to access '...': Failed to connect to ...: Connection refused",
        )
        with patch.object(mod, "run", side_effect=fake_run):
            rc = mod.main([".", "3", "test-slug"])

        assert rc == 0

    def test_merge_conflict_still_fails(self, capsys):
        """Non-network pull failures (merge conflicts, divergent histories) must
        still exit 2 — the fallback is network-specific."""
        mod = _load_module()
        fake_run, _ = _fake_run_with_pull(
            pull_rc=1,
            pull_stderr=(
                "CONFLICT (content): Merge conflict in foo.py\n"
                "Automatic merge failed; fix conflicts and then commit the result."
            ),
        )
        with patch.object(mod, "run", side_effect=fake_run):
            rc = mod.main([".", "4", "test-slug"])

        assert rc == 2
        _, err = capsys.readouterr()
        assert "git pull origin main failed" in err

    def test_stash_is_popped_after_unreachable_pull(self, capsys):
        """When dirty-state triggered an auto-stash and origin is unreachable,
        the stash must still be popped onto the new branch."""
        mod = _load_module()
        fake_run, stash_pops = _fake_run_with_pull(
            pull_rc=128,
            pull_stderr="fatal: unable to access '...': Could not resolve host: nowhere",
            dirty=True,
        )
        with patch.object(mod, "run", side_effect=fake_run):
            rc = mod.main([".", "5", "test-slug"])

        assert rc == 0
        assert len(stash_pops) == 1, (
            f"expected exactly one `git stash pop` after successful branch creation; "
            f"got {stash_pops}"
        )


class TestIsRemoteUnreachable:
    """Unit tests for the _is_remote_unreachable heuristic."""

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
