"""Unit tests for stash-restore warning in tusk-branch.py (Issue #432).

When tusk branch restores stashed changes via git stash pop, it should list
the restored file paths and warn the developer they may belong to a different task.
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


def _make_dirty_run(restored_files: list[str]):
    """Return a fake run() simulating a dirty working tree with stash-restored files."""

    def fake_run(args, check=True):
        if args[:3] == ["git", "remote", "set-head", "origin", "--auto"]:
            return _cp(0)
        if args[:2] == ["git", "symbolic-ref"]:
            return _cp(0, stdout="refs/remotes/origin/main\n")
        if args[:2] == ["git", "status"] and "--porcelain" in args:
            # First call: dirty check (before stash); subsequent calls: restored files
            if not hasattr(fake_run, "_status_calls"):
                fake_run._status_calls = 0
            fake_run._status_calls += 1
            if fake_run._status_calls == 1:
                # Dirty working tree triggers stash
                return _cp(0, stdout="M  alerting_adapter.py\n")
            else:
                # After stash pop: list of restored files
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


class TestStashRestoreWarning:
    """_try_pop_stash emits a file list and different-task warning when files are restored."""

    def test_restored_files_appear_in_warning(self, capsys):
        mod = _load_module()
        fake_run = _make_dirty_run(["alerting_adapter.py", "utils/helpers.py"])

        with patch.object(mod, "run", side_effect=fake_run):
            rc = mod.main([".", "68", "test-slug"])

        assert rc == 0
        err = capsys.readouterr().err
        assert "alerting_adapter.py" in err
        assert "utils/helpers.py" in err

    def test_different_task_warning_message(self, capsys):
        mod = _load_module()
        fake_run = _make_dirty_run(["some_file.py"])

        with patch.object(mod, "run", side_effect=fake_run):
            mod.main([".", "68", "test-slug"])

        err = capsys.readouterr().err
        assert "different task" in err

    def test_no_file_listing_when_status_empty(self, capsys):
        """When status returns no files after pop, fall back to generic note."""
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
                    return _cp(0, stdout="M  something.py\n")  # dirty
                return _cp(0, stdout="")  # nothing after pop
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
            rc = mod.main([".", "68", "test-slug"])

        assert rc == 0
        err = capsys.readouterr().err
        # Should fall back to the generic note, not the different-task warning
        assert "different task" not in err
        assert "stash restored" in err
