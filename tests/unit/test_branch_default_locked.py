"""Unit tests for tusk-branch.py when the default branch is locked elsewhere."""

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


def test_creates_branch_from_origin_main_without_checking_out_locked_default(capsys):
    """A linked worktree may already have main checked out, so branch creation
    must not require checking main out in the current worktree first."""
    mod = _load_module()
    calls: list[list[str]] = []

    def fake_run(args, check=True):
        calls.append(args)
        if args[:4] == ["git", "remote", "set-head", "origin"]:
            return _cp(0)
        if args[:2] == ["git", "symbolic-ref"]:
            return _cp(0, stdout="refs/remotes/origin/main\n")
        if args[:2] == ["git", "status"]:
            return _cp(0, stdout="")
        if args[:3] == ["git", "remote", "get-url"]:
            return _cp(0, stdout="https://github.com/test/repo.git\n")
        if args[:2] == ["git", "fetch"]:
            return _cp(0)
        if args[:3] == ["git", "branch", "--list"]:
            return _cp(0, stdout="")
        if args[:2] == ["git", "checkout"] and "-b" in args:
            return _cp(0)
        return _cp(0)

    with patch.object(mod, "run", side_effect=fake_run):
        rc = mod.main([".", "42", "locked-default"])

    assert rc == 0
    out, _ = capsys.readouterr()
    assert "feature/TASK-42-locked-default" in out
    assert ["git", "checkout", "main"] not in calls
    assert ["git", "checkout", "-b", "feature/TASK-42-locked-default", "origin/main"] in calls


def test_existing_task_branch_locked_elsewhere_fails_before_stashing(capsys):
    mod = _load_module()
    calls: list[list[str]] = []
    existing_branch = "feature/TASK-42-locked-default"

    def fake_run(args, check=True):
        calls.append(args)
        if args[:4] == ["git", "remote", "set-head", "origin"]:
            return _cp(0)
        if args[:2] == ["git", "symbolic-ref"]:
            return _cp(0, stdout="refs/remotes/origin/main\n")
        if args[:2] == ["git", "status"]:
            return _cp(0, stdout=" M unrelated-task-file.py\n")
        if args[:3] == ["git", "remote", "get-url"]:
            return _cp(0, stdout="https://github.com/test/repo.git\n")
        if args[:2] == ["git", "fetch"]:
            return _cp(0)
        if args[:3] == ["git", "branch", "--list"]:
            return _cp(0, stdout=f"  {existing_branch}\n")
        if args[:4] == ["git", "worktree", "list", "--porcelain"]:
            return _cp(
                0,
                stdout=(
                    "worktree /tmp/other-task\n"
                    "HEAD abc123\n"
                    f"branch refs/heads/{existing_branch}\n"
                ),
            )
        return _cp(0)

    with patch.object(mod, "run", side_effect=fake_run):
        rc = mod.main([".", "42", "locked-default"])

    assert rc == 2
    _, err = capsys.readouterr()
    assert "/tmp/other-task" in err
    assert existing_branch in err
    assert not [c for c in calls if c[:3] == ["git", "stash", "push"]]
