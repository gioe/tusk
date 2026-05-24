"""Unit tests for _rebase_in_feature_worktree (issue #858).

When the feature branch lives in a task worktree (the normal /tusk flow),
the primary checkout cannot ``git checkout`` it — git refuses with "is
already used by worktree at <path>". The fix routes the rebase via
``git -C <worktree_path>`` so the primary checkout stays on the default
branch and the feature branch's ref is still updated in place.

These tests pin three invariants:
  1. The success path invokes ``git -C <worktree> rebase <target>`` —
     never bare ``git rebase`` and never ``git checkout``.
  2. The failure path's error message names the feature worktree path
     and tells the operator to ``cd`` there to resolve conflicts.
  3. None-path branch (no linked worktree) is NOT tested here — the
     existing in-place rebase tests under tests/unit/test_merge_*.py
     already cover that path.
"""

import importlib.util
import io
import os
import subprocess
from contextlib import redirect_stderr
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
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


class TestRebaseInFeatureWorktree:
    def test_success_invokes_git_dash_c_and_returns_zero(self):
        """The rebase call MUST go through `git -C <worktree>` — not bare git."""
        mod = _load_module()
        calls = []

        def fake_run(args, check=True):
            calls.append(list(args))
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = mod._rebase_in_feature_worktree(
                    worktree_path="/Users/x/.tusk/worktrees/TASK-N-foo",
                    branch_name="feature/TASK-N-foo",
                    rebase_target="origin/main",
                    task_id=42,
                    did_stash=False,
                )
        assert rc == 0
        assert len(calls) == 1
        # The shape of the command — issue #858's whole point — is what we pin.
        assert calls[0] == [
            "git",
            "-C",
            "/Users/x/.tusk/worktrees/TASK-N-foo",
            "rebase",
            "origin/main",
        ]
        # MUST NOT have called `git checkout` anywhere.
        assert not any(c[:2] == ["git", "checkout"] for c in calls)

    def test_failure_surfaces_worktree_path_in_recovery_hint(self):
        """The error message must name the worktree path so the operator
        knows where to cd to resolve conflicts. Without this hint the
        operator runs `git status` in the primary checkout, sees a clean
        tree, and concludes nothing went wrong — the rebase-in-progress
        state actually lives in the linked worktree."""
        mod = _load_module()
        with patch.object(
            mod,
            "run",
            return_value=_cp(1, stderr="error: could not apply abc123..."),
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = mod._rebase_in_feature_worktree(
                    worktree_path="/Users/x/.tusk/worktrees/TASK-99-bar",
                    branch_name="feature/TASK-99-bar",
                    rebase_target="origin/main",
                    task_id=99,
                    did_stash=False,
                )
        assert rc == 2
        stderr_out = err.getvalue()
        assert "/Users/x/.tusk/worktrees/TASK-99-bar" in stderr_out
        assert "cd " in stderr_out  # tells the operator to cd
        assert "rebase --continue" in stderr_out
        assert "rebase --abort" in stderr_out

    def test_failure_with_did_stash_includes_stash_note(self):
        """When did_stash is True, the failure message must include the
        stash-restoration note (parallel to the in-place rebase failure)."""
        mod = _load_module()
        with patch.object(
            mod, "run", return_value=_cp(1, stderr="conflict")
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = mod._rebase_in_feature_worktree(
                    worktree_path="/tmp/wt",
                    branch_name="feature/TASK-7-z",
                    rebase_target="origin/main",
                    task_id=7,
                    did_stash=True,
                )
        assert rc == 2
        stderr_out = err.getvalue()
        assert "auto-stash for TASK-7" in stderr_out
        assert "git stash pop" in stderr_out

    def test_failure_without_did_stash_omits_stash_note(self):
        mod = _load_module()
        with patch.object(
            mod, "run", return_value=_cp(1, stderr="conflict")
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = mod._rebase_in_feature_worktree(
                    worktree_path="/tmp/wt",
                    branch_name="feature/TASK-7-z",
                    rebase_target="origin/main",
                    task_id=7,
                    did_stash=False,
                )
        assert rc == 2
        stderr_out = err.getvalue()
        assert "auto-stash for TASK-7" not in stderr_out
