"""Unit tests for tusk-branch.py stash handling.

Default behavior (TASK-120): when the working tree is dirty, tusk branch
auto-stashes, switches branches, and **leaves the stash intact** — printing
the stash ref and message so the user can retrieve the orphan changes
manually. Pop-onto-new-branch behavior is opt-in via --pop-stash.

History: the prior default (auto-pop the stash onto the new feature branch)
was unsafe when the orphan changes belonged to a previous task, a common
case during /loop or resumed sessions. Tests below pin the new default and
cover the --pop-stash opt-in that restores the old behavior.
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


def _make_dirty_run(restored_files: list[str] | None = None, record: list | None = None):
    """Fake subprocess.run simulating a dirty tree on a successful branch switch.

    The first `git status --porcelain` call returns dirty; subsequent calls
    (after a hypothetical pop) return `restored_files`. `record`, if given,
    captures every (cmd-prefix-tuple) invocation so tests can assert whether
    `git stash pop` was reached.
    """
    restored_files = restored_files or []

    def fake_run(args, check=True):
        if record is not None:
            record.append(tuple(args))
        if args[:3] == ["git", "remote", "set-head", "origin", "--auto"]:
            return _cp(0)
        if args[:2] == ["git", "symbolic-ref"]:
            return _cp(0, stdout="refs/remotes/origin/main\n")
        if args[:2] == ["git", "status"] and "--porcelain" in args:
            if not hasattr(fake_run, "_status_calls"):
                fake_run._status_calls = 0
            fake_run._status_calls += 1
            if fake_run._status_calls == 1:
                return _cp(0, stdout="M  alerting_adapter.py\n")
            lines = "".join(f"M  {f}\n" for f in restored_files)
            return _cp(0, stdout=lines)
        if args[:2] == ["git", "stash"] and "push" in args:
            return _cp(0, stdout="Saved working directory and index state")
        if args == ["git", "checkout", "main"]:
            return _cp(0)
        if args[:3] == ["git", "pull"]:
            return _cp(0)
        if args[:3] == ["git", "branch", "--list"]:
            return _cp(0, stdout="")
        if args[:2] == ["git", "checkout"] and "-b" in args:
            return _cp(0)
        if args[:2] == ["git", "stash"] and "pop" in args:
            return _cp(0)
        return _cp(0)

    return fake_run


class TestDefaultLeavesStashIntact:
    """By default, tusk branch must NOT pop the auto-stash onto the new branch."""

    def test_default_does_not_call_stash_pop(self, capsys):
        mod = _load_module()
        calls: list[tuple] = []
        fake_run = _make_dirty_run(record=calls)

        with patch.object(mod, "run", side_effect=fake_run):
            rc = mod.main([".", "68", "test-slug"])

        assert rc == 0
        pop_calls = [c for c in calls if c[:3] == ("git", "stash", "pop")]
        assert pop_calls == [], f"default path must not invoke git stash pop, got: {pop_calls}"

    def test_default_prints_stash_ref_and_message(self, capsys):
        """The orphan-stash note must name stash@{0} and the stash message so
        the user can restore or drop it manually."""
        mod = _load_module()
        fake_run = _make_dirty_run()

        with patch.object(mod, "run", side_effect=fake_run):
            rc = mod.main([".", "68", "test-slug"])

        assert rc == 0
        err = capsys.readouterr().err
        assert "stash@{0}" in err
        assert "tusk-branch: auto-stash for TASK-68" in err
        assert "git stash pop" in err  # instructs user how to restore

    def test_default_does_not_emit_legacy_different_task_warning(self, capsys):
        """The old 'these changes may belong to a different task' warning only
        fires when we actually pop; default path must not print it."""
        mod = _load_module()
        fake_run = _make_dirty_run(restored_files=["alerting_adapter.py"])

        with patch.object(mod, "run", side_effect=fake_run):
            rc = mod.main([".", "68", "test-slug"])

        assert rc == 0
        err = capsys.readouterr().err
        assert "different task" not in err


class TestPopStashFlagRestoresLegacyBehavior:
    """--pop-stash restores the pre-TASK-120 behavior (auto-pop onto new branch)."""

    def test_pop_stash_flag_calls_git_stash_pop(self, capsys):
        mod = _load_module()
        calls: list[tuple] = []
        fake_run = _make_dirty_run(
            restored_files=["alerting_adapter.py"],
            record=calls,
        )

        with patch.object(mod, "run", side_effect=fake_run):
            rc = mod.main([".", "68", "test-slug", "--pop-stash"])

        assert rc == 0
        pop_calls = [c for c in calls if c[:3] == ("git", "stash", "pop")]
        assert len(pop_calls) == 1, f"--pop-stash must invoke git stash pop once, got: {pop_calls}"

    def test_pop_stash_flag_emits_restored_file_list(self, capsys):
        mod = _load_module()
        fake_run = _make_dirty_run(
            restored_files=["alerting_adapter.py", "utils/helpers.py"],
        )

        with patch.object(mod, "run", side_effect=fake_run):
            rc = mod.main([".", "68", "test-slug", "--pop-stash"])

        assert rc == 0
        err = capsys.readouterr().err
        assert "alerting_adapter.py" in err
        assert "utils/helpers.py" in err
        assert "different task" in err

    def test_pop_stash_flag_no_file_listing_when_status_empty(self, capsys):
        """When git status returns empty after pop, fall back to generic note
        (preserved from the legacy implementation)."""
        mod = _load_module()

        def fake_run(args, check=True):
            if args[:3] == ["git", "remote", "set-head", "origin", "--auto"]:
                return _cp(0)
            if args[:2] == ["git", "symbolic-ref"]:
                return _cp(0, stdout="refs/remotes/origin/main\n")
            if args[:2] == ["git", "status"] and "--porcelain" in args:
                if not hasattr(fake_run, "_calls"):
                    fake_run._calls = 0
                fake_run._calls += 1
                if fake_run._calls == 1:
                    return _cp(0, stdout="M  something.py\n")
                return _cp(0, stdout="")
            if args[:2] == ["git", "stash"] and "push" in args:
                return _cp(0)
            if args == ["git", "checkout", "main"]:
                return _cp(0)
            if args[:3] == ["git", "pull"]:
                return _cp(0)
            if args[:3] == ["git", "branch", "--list"]:
                return _cp(0, stdout="")
            if args[:2] == ["git", "checkout"] and "-b" in args:
                return _cp(0)
            if args[:2] == ["git", "stash"] and "pop" in args:
                return _cp(0)
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            rc = mod.main([".", "68", "test-slug", "--pop-stash"])

        assert rc == 0
        err = capsys.readouterr().err
        assert "different task" not in err
        assert "stash restored" in err
