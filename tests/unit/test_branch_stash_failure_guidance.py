"""Unit tests for tusk-branch.py dirty-worktree stash failure guidance."""

import importlib.util
import os
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BRANCH_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-branch.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_branch", BRANCH_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_stash_index_failure_points_to_task_worktree_fallback(monkeypatch, capsys):
    mod = _load_module()

    def fake_run(args, check=True):
        if args[:3] == ["git", "remote", "set-head"]:
            return _cp(0)
        if args[:3] == ["git", "symbolic-ref", "refs/remotes/origin/HEAD"]:
            return _cp(0, stdout="refs/remotes/origin/main\n")
        if args[:3] == ["git", "status", "--porcelain"]:
            return _cp(0, stdout=" M README.md\n")
        if args[:3] == ["git", "stash", "push"]:
            return _cp(1, stderr="error: could not write index\n")
        if args[:3] == ["git", "show-ref", "--verify"]:
            return _cp(1)
        return _cp(0)

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(mod, "checkpoint_wal", lambda _db_path: None)
    monkeypatch.setattr(mod, "_warn_branch_auto_stash_residue", lambda _repo_root: None)

    rc = mod.main(["/repo", "42", "fix-precheck"])
    err = capsys.readouterr().err

    assert rc == 2
    assert "could not write index" in err
    assert "tusk task-worktree create 42 fix-precheck" in err
