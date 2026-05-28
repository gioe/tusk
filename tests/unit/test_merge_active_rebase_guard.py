"""Unit tests for the active-rebase worktree-removal guard (issue #940).

A parallel session running ``tusk merge`` for the same task used to delete the
sibling task worktree even while it held an active rebase, destroying the
operator's in-progress conflict resolution. ``_remove_recorded_task_worktree``
now refuses removal when ``_worktree_has_active_rebase`` reports a rebase in
progress, so the worktree (and that work) survives.

The detection tests use a REAL temporary git worktree placed into a REAL paused
rebase — the guard short-circuits before any DB access, so the refusal path can
be exercised against the real function with a hand-built workspace row and no
database.
"""

import importlib.util
import io
import os
import subprocess
from contextlib import redirect_stderr
from unittest.mock import MagicMock, patch


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


def _git(args, cwd):
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
    )
    return r


def _cp(returncode, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _setup_paused_rebase(tmp_path):
    """Build a real repo + linked worktree and leave the worktree in a paused
    rebase (conflict). Returns (repo_path, worktree_path, branch)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(["init", "-b", "main"], repo).returncode == 0
    _git(["config", "user.email", "tusk@example.test"], repo)
    _git(["config", "user.name", "Tusk Tests"], repo)
    (repo / "conflict.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "conflict.txt"], repo)
    assert _git(["commit", "-m", "base"], repo).returncode == 0

    branch = "feature/TASK-525-demo"
    wt = tmp_path / "wt"
    assert _git(["worktree", "add", "-b", branch, str(wt), "main"], repo).returncode == 0

    # Advance main with a conflicting change.
    (repo / "conflict.txt").write_text("main\n", encoding="utf-8")
    _git(["add", "conflict.txt"], repo)
    assert _git(["commit", "-m", "main change"], repo).returncode == 0

    # Diverge the feature branch with a conflicting change on the same line.
    (wt / "conflict.txt").write_text("feature\n", encoding="utf-8")
    _git(["add", "conflict.txt"], wt)
    assert _git(["commit", "-m", "feature change"], wt).returncode == 0

    # Rebase feature onto main → conflict → rebase pauses (rebase-merge dir).
    rebase = _git(["rebase", "main"], wt)
    assert rebase.returncode != 0, "expected the rebase to stop at a conflict"
    return repo, wt, branch


class TestWorktreeHasActiveRebase:
    def test_detects_paused_rebase_in_linked_worktree(self, tmp_path):
        mod = _load_module()
        _repo, wt, _branch = _setup_paused_rebase(tmp_path)
        assert mod._worktree_has_active_rebase(str(wt)) is True

    def test_clean_checkout_reports_no_rebase(self, tmp_path):
        mod = _load_module()
        repo, _wt, _branch = _setup_paused_rebase(tmp_path)
        # The main checkout has no rebase in progress.
        assert mod._worktree_has_active_rebase(str(repo)) is False

    def test_missing_path_reports_no_rebase(self, tmp_path):
        mod = _load_module()
        assert mod._worktree_has_active_rebase(str(tmp_path / "nope")) is False


class TestRemoveRefusesActiveRebase:
    def test_refuses_removal_and_worktree_survives(self, tmp_path):
        mod = _load_module()
        _repo, wt, branch = _setup_paused_rebase(tmp_path)
        workspace = {"id": 1, "branch": branch, "workspace_path": str(wt)}

        err = io.StringIO()
        with redirect_stderr(err):
            # db_path is unused: the rebase guard short-circuits before any DB
            # access, symlink cleanup, or `git worktree remove`.
            result = mod._remove_recorded_task_worktree(
                db_path="/nonexistent/tusk/tasks.db",
                task_id=525,
                branch_name=branch,
                workspace=workspace,
            )

        assert result is False
        assert os.path.isdir(str(wt)), "worktree must survive the refused removal"
        message = err.getvalue()
        assert "rebase is in progress" in message
        assert str(wt) in message

    def test_no_rebase_proceeds_to_remove(self, tmp_path):
        """Criterion: clean-worktree removal is preserved when no rebase is in
        progress. Mock the git/DB side effects and assert the function falls
        through to `git worktree remove` and returns True."""
        mod = _load_module()
        wt = tmp_path / "clean-wt"
        wt.mkdir()
        branch = "feature/TASK-525-clean"
        workspace = {"id": 7, "branch": branch, "workspace_path": str(wt)}

        calls = []

        def fake_run(args, check=True):
            calls.append(list(args))
            # rev-parse --git-path returns empty (no rebase dir); worktree
            # remove succeeds.
            return _cp(0, stdout="")

        with patch.object(mod, "run", side_effect=fake_run), patch.object(
            mod, "_clean_tusk_auto_symlinks", return_value=0
        ), patch.object(mod, "_forget_task_workspace"):
            result = mod._remove_recorded_task_worktree(
                db_path="/unused/tusk/tasks.db",
                task_id=525,
                branch_name=branch,
                workspace=workspace,
            )

        assert result is True
        assert ["git", "worktree", "remove", str(wt)] in calls
