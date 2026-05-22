"""Integration tests for `tusk abandon` (TASK-49, TASK-305).

`tusk abandon` is the no-commit symmetric of `tusk merge`: it closes a task
with closed_reason in (wont_do, duplicate, completed), force-deletes the
feature branch when one exists, closes the open session, and emits JSON in
the same shape as `tusk merge`.

Exercises:
  - All three abandon reasons (wont_do, duplicate, completed) correctly close
    the task and the session. `completed` is the convergent-completion path
    (issue #580): the goal was met by separate work landing on the default
    branch, so there are no commits to ship.
  - Refuses (exit code 2) when the feature branch has commits not on the
    default branch, with an error pointing the user at `tusk merge`.
  - Rejects reasons that aren't in the abandon set (e.g. `expired`, garbage).
  - Optional `--note` is persisted to task_progress so the rationale survives —
    for `--reason completed`, that note is the audit signal that distinguishes
    convergent-completion from a normal merge close.
"""

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr, redirect_stdout

import pytest

from tests.integration.conftest import _insert_session, _insert_task

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        os.path.join(REPO_ROOT, "bin", f"{name}.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_abandon = _load("tusk-abandon")


def _call(db_path, config_path, *args) -> tuple[int, dict | None, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_abandon.main([str(db_path), str(config_path), *[str(a) for a in args]])
    out = out_buf.getvalue().strip()
    parsed: dict | None = None
    if out:
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            parsed = None
    return rc, parsed, err_buf.getvalue()


class TestAbandonHappyPath:
    """All three abandon reasons close the task and the open session in one call."""

    @pytest.mark.parametrize("reason", ["wont_do", "duplicate", "completed"])
    def test_abandon_closes_task_and_session(
        self, db_path, config_path, monkeypatch, reason
    ):
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        # No feature branch exists; treat as nothing-to-clean-up so the test
        # doesn't depend on git state inside the test repo.
        monkeypatch.setattr(
            tusk_abandon,
            "find_task_branch",
            lambda tid: (None, f"No branch found matching feature/TASK-{tid}-*", False),
        )
        monkeypatch.setattr(tusk_abandon, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_abandon, "checkpoint_wal", lambda db: None)

        rc, result, stderr = _call(
            db_path,
            config_path,
            task_id,
            "--reason",
            reason,
            "--session",
            session_id,
        )

        assert rc == 0, f"abandon failed: {stderr}"
        assert result is not None, f"expected JSON on stdout; stderr was:\n{stderr}"
        assert result["task"]["status"] == "Done"
        assert result["task"]["closed_reason"] == reason
        assert result["sessions_closed"] == 1

        # Verify the session is actually closed in the DB
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT ended_at FROM task_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            assert row[0] is not None, "session should be closed"
        finally:
            conn.close()

    def test_abandon_with_note_persists_to_task_progress(
        self, db_path, config_path, monkeypatch
    ):
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        monkeypatch.setattr(
            tusk_abandon,
            "find_task_branch",
            lambda tid: (None, f"No branch found matching feature/TASK-{tid}-*", False),
        )
        monkeypatch.setattr(tusk_abandon, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_abandon, "checkpoint_wal", lambda db: None)

        rc, _, stderr = _call(
            db_path,
            config_path,
            task_id,
            "--reason",
            "wont_do",
            "--session",
            session_id,
            "--note",
            "Spike concluded the design is wrong.",
        )

        assert rc == 0, f"abandon failed: {stderr}"

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT commit_message FROM task_progress WHERE task_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            assert row is not None, "expected a task_progress row for the note"
            assert "[abandon: wont_do]" in row[0]
            assert "Spike concluded the design is wrong." in row[0]
        finally:
            conn.close()


class TestAbandonRefusesUnmergedCommits:
    """Guard: a feature branch with commits not on default must not be deleted."""

    def test_allows_branch_with_only_unrelated_task_commits(
        self, db_path, config_path, monkeypatch
    ):
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch_name = f"feature/TASK-{task_id}-thing"
        unrelated_task_id = task_id + 999
        workspace = {
            "branch": branch_name,
            "workspace_path": "/tmp/TASK-unrelated-commits",
        }
        monkeypatch.setattr(
            tusk_abandon, "_recorded_task_workspace", lambda db, tid: workspace
        )
        monkeypatch.setattr(tusk_abandon, "_branch_exists", lambda branch: True)
        monkeypatch.setattr(
            tusk_abandon, "_remove_recorded_task_worktree", lambda *args, **kwargs: True
        )
        monkeypatch.setattr(tusk_abandon, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_abandon, "checkpoint_wal", lambda db: None)

        def _passthrough(args, check=True):
            return subprocess.run(
                args, capture_output=True, text=True, encoding="utf-8", check=check
            )

        def _mock_run(args, check=True):
            if not args or args[0] != "git":
                return _passthrough(args, check=check)
            if args[:2] == ["git", "log"] and "--not" in args:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=f"abc1234 [TASK-{unrelated_task_id}] sibling work\n",
                    stderr="",
                )
            if args[:2] == ["git", "cherry"]:
                return subprocess.CompletedProcess(
                    args, 0, stdout="+ abc1234\n", stderr=""
                )
            if args[:3] == ["git", "branch", "-D"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:3] == ["git", "stash", "list"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_abandon, "run", _mock_run)
        monkeypatch.setattr(tusk_abandon._merge, "run", _mock_run)

        rc, result, stderr = _call(
            db_path,
            config_path,
            task_id,
            "--reason",
            "wont_do",
            "--session",
            session_id,
        )

        assert rc == 0, f"abandon failed: {stderr}"
        assert result is not None
        assert result["task"]["status"] == "Done"
        assert result["sessions_closed"] == 1

    def test_refuses_when_branch_has_unmerged_commits(
        self, db_path, config_path, monkeypatch
    ):
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        monkeypatch.setattr(
            tusk_abandon,
            "find_task_branch",
            lambda tid: (f"feature/TASK-{tid}-thing", None, False),
        )
        monkeypatch.setattr(tusk_abandon, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_abandon, "checkpoint_wal", lambda db: None)

        # Simulate a feature branch that has one exclusive commit not on main
        # (and which has NOT been cherry-picked).
        def _mock_run(args, check=True):
            if args[:2] == ["git", "log"] and "--not" in args:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=f"abc1234 [TASK-{task_id}] some unmerged work\n",
                    stderr="",
                )
            if args[:2] == ["git", "cherry"]:
                # '+' lines mean "patch is on feature but NOT on default"
                return subprocess.CompletedProcess(
                    args, 0, stdout="+ abc1234\n", stderr=""
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_abandon, "run", _mock_run)

        rc, result, stderr = _call(
            db_path,
            config_path,
            task_id,
            "--reason",
            "wont_do",
            "--session",
            session_id,
        )

        assert rc == 2, f"expected exit 2, got {rc}; stderr:\n{stderr}"
        assert result is None
        assert "tusk merge" in stderr, "error must point user at tusk merge"
        assert f"feature/TASK-{task_id}-thing" in stderr

        # The task and session must remain open — abandon must not partially
        # close anything when the branch guard fires.
        conn = sqlite3.connect(str(db_path))
        try:
            task_row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            session_row = conn.execute(
                "SELECT ended_at FROM task_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        finally:
            conn.close()
        assert task_row[0] != "Done", "task must NOT be closed when guard fires"
        assert session_row[0] is None, "session must NOT be closed when guard fires"


class TestAbandonReasonValidation:
    """`--reason` must be one of the no-commit reasons; everything else fails fast."""

    @pytest.mark.parametrize("bad_reason", ["expired", "garbage", "converged"])
    def test_rejects_non_abandon_reasons(self, db_path, config_path, bad_reason):
        rc, result, stderr = _call(
            db_path, config_path, 1, "--reason", bad_reason
        )
        assert rc == 1
        assert result is None
        assert "wont_do|duplicate|completed" in stderr

    def test_rejects_missing_reason(self, db_path, config_path):
        rc, _, stderr = _call(db_path, config_path, 1)
        assert rc == 1
        assert "--reason" in stderr


class TestAbandonCompletedConvergent:
    """Issue #580 — `--reason completed` is the convergent-completion path.

    When a task's goal was already met by separate work landing on the default
    branch between filing and pickup, there are no commits to ship via
    `tusk merge`. `tusk abandon --reason completed` closes the task with
    `closed_reason = 'completed'` (matching what `tusk merge` would have
    written) and records the rationale on `task_progress` so future readers
    can see *why* there were no commits.
    """

    def test_abandon_reason_completed_closes_task_done(
        self, db_path, config_path, monkeypatch
    ):
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        monkeypatch.setattr(
            tusk_abandon,
            "find_task_branch",
            lambda tid: (None, f"No branch found matching feature/TASK-{tid}-*", False),
        )
        monkeypatch.setattr(tusk_abandon, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_abandon, "checkpoint_wal", lambda db: None)

        rc, result, stderr = _call(
            db_path,
            config_path,
            task_id,
            "--reason",
            "completed",
            "--session",
            session_id,
            "--note",
            "Goal met by TASK-1727, TASK-1730, TASK-1763 (convergent refactors).",
        )

        assert rc == 0, f"abandon --reason completed failed: {stderr}"
        assert result is not None, f"expected JSON on stdout; stderr was:\n{stderr}"
        assert result["task"]["status"] == "Done"
        assert result["task"]["closed_reason"] == "completed", (
            "convergent-completion must record closed_reason='completed' so the "
            "DB reads the same as a normal `tusk merge` close"
        )
        assert result["sessions_closed"] == 1

        conn = sqlite3.connect(str(db_path))
        try:
            session_row = conn.execute(
                "SELECT ended_at FROM task_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            assert session_row[0] is not None, "session should be closed"

            # The audit-trail signal: [abandon: completed] note distinguishes
            # this case from a normal `tusk merge` close (which never writes a
            # task_progress row with the [abandon: ...] prefix).
            note_row = conn.execute(
                "SELECT commit_message FROM task_progress WHERE task_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            assert note_row is not None, "expected a task_progress row for the note"
            assert "[abandon: completed]" in note_row[0]
            assert "TASK-1727" in note_row[0]
        finally:
            conn.close()


class TestAbandonLinkedWorktree:
    """Issue #677: unrecorded linked worktrees should not surface raw checkout conflicts."""

    def test_checkout_conflict_mentions_linked_worktree_recovery(
        self, db_path, config_path, monkeypatch
    ):
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch_name = f"feature/TASK-{task_id}-manual-worktree"
        monkeypatch.setattr(tusk_abandon, "_recorded_task_workspace", lambda db, tid: None)
        monkeypatch.setattr(
            tusk_abandon,
            "find_task_branch",
            lambda tid: (branch_name, None, False),
        )
        monkeypatch.setattr(tusk_abandon, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(
            tusk_abandon,
            "_branch_has_unmerged_commits",
            lambda branch, default, tid: (False, None),
        )
        monkeypatch.setattr(tusk_abandon, "checkpoint_wal", lambda db: None)

        def fake_run(args, check=True):
            if args == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, stdout=branch_name + "\n", stderr="")
            if args == ["git", "checkout", "main"]:
                return subprocess.CompletedProcess(
                    args,
                    128,
                    stdout="",
                    stderr="fatal: 'main' is already used by worktree at '/tmp/primary'\n",
                )
            raise AssertionError(f"unexpected command: {args}")

        monkeypatch.setattr(tusk_abandon, "run", fake_run)

        rc, result, stderr = _call(
            db_path,
            config_path,
            task_id,
            "--reason",
            "completed",
            "--session",
            session_id,
        )

        assert rc == 2
        assert result is None
        assert "linked worktree" in stderr
        assert "primary checkout" in stderr
        assert "task-worktree" in stderr
        assert "fatal: 'main' is already used by worktree" in stderr


class TestAbandonInternalTuskInvocation:
    def test_missing_tusk_wrapper_during_session_close_reports_actionable_error(
        self, db_path, config_path, monkeypatch
    ):
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        monkeypatch.setattr(
            tusk_abandon,
            "find_task_branch",
            lambda tid: (None, f"No branch found matching feature/TASK-{tid}-*", False),
        )
        monkeypatch.setattr(tusk_abandon, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_abandon, "checkpoint_wal", lambda db: None)

        def _mock_run(args, check=True):
            if "session-close" in args:
                raise FileNotFoundError(os.path.join(os.path.dirname(__file__), "tusk"))
            if args[:3] == ["git", "stash", "list"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_abandon, "run", _mock_run)
        monkeypatch.setattr(tusk_abandon._merge, "run", _mock_run)

        rc, result, stderr = _call(
            db_path,
            config_path,
            task_id,
            "--reason",
            "completed",
            "--session",
            session_id,
        )

        assert rc == 2
        assert result is None
        assert "project-local tusk binary disappeared during closeout" in stderr
        assert "retry after any install or upgrade finishes" in stderr


class TestAbandonRecordedWorktreeCleanup:
    def test_recorded_worktree_removed_after_session_and_task_close(
        self, db_path, config_path, tmp_path, monkeypatch
    ):
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
            branch_name = f"feature/TASK-{task_id}-recorded-worktree"
            workspace_path = tmp_path / "TASK-recorded-worktree"
            workspace_path.mkdir()
            conn.execute(
                "INSERT INTO task_workspaces (task_id, branch, workspace_path) "
                "VALUES (?, ?, ?)",
                (task_id, branch_name, str(workspace_path)),
            )
            conn.commit()
        finally:
            conn.close()

        calls: list[list[str]] = []

        def _mock_run(args, check=True):
            calls.append(list(args))
            if "session-close" in args:
                return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")
            if "task-done" in args:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=json.dumps(
                        {
                            "task": {
                                "id": task_id,
                                "status": "Done",
                                "closed_reason": "completed",
                            },
                            "sessions_closed": 0,
                            "unblocked_tasks": [],
                        }
                    ),
                    stderr="",
                )
            if args[:3] == ["git", "worktree", "remove"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:3] == ["git", "branch", "-D"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:3] == ["git", "stash", "list"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_abandon, "_branch_exists", lambda branch: True)
        monkeypatch.setattr(
            tusk_abandon,
            "_branch_has_unmerged_commits",
            lambda branch, default, tid: (False, None),
        )
        monkeypatch.setattr(tusk_abandon, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_abandon, "checkpoint_wal", lambda db: None)
        monkeypatch.setattr(tusk_abandon, "run", _mock_run)
        monkeypatch.setattr(tusk_abandon._merge, "run", _mock_run)

        rc, result, stderr = _call(
            db_path,
            config_path,
            task_id,
            "--reason",
            "completed",
            "--session",
            session_id,
        )

        assert rc == 0, f"abandon failed: {stderr}"
        assert result is not None

        session_idx = next(i for i, c in enumerate(calls) if "session-close" in c)
        done_idx = next(i for i, c in enumerate(calls) if "task-done" in c)
        remove_idx = next(
            i for i, c in enumerate(calls) if c[:3] == ["git", "worktree", "remove"]
        )
        assert session_idx < done_idx < remove_idx


class TestAbandonPreservesBranchAutoStash:
    """Issue #727.

    `tusk branch <id>` auto-stashes a dirty working tree under
    `tusk-branch: auto-stash for TASK-<id>` so it can drop the user back on
    the default branch cleanly. Abandon must not silently drop that stash on
    no-commit close-out paths; it must leave the stash intact and tell the user
    exactly how to restore or remove it.
    """

    def test_abandon_preserves_branch_auto_stash_after_branch_delete(
        self, db_path, config_path, monkeypatch
    ):
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        branch_name = f"feature/TASK-{task_id}-thing"

        monkeypatch.setattr(
            tusk_abandon,
            "find_task_branch",
            lambda tid: (branch_name, None, False),
        )
        monkeypatch.setattr(tusk_abandon, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_abandon, "checkpoint_wal", lambda db: None)

        # Mock git invocations only; pass tusk subprocess calls through to the
        # real binary so `tusk task-done` etc. actually mark the task Done in
        # the test DB.
        calls: list[list[str]] = []

        def _passthrough(args, check=True):
            return subprocess.run(
                args, capture_output=True, text=True, encoding="utf-8", check=check
            )

        def _mock_run(args, check=True):
            calls.append(args)
            if not args or args[0] != "git":
                return _passthrough(args, check=check)
            # Branch has no unmerged commits (so abandon proceeds to delete).
            if args[:2] == ["git", "log"] and "--not" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:2] == ["git", "cherry"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            # We start out on the feature branch so abandon checks out main.
            if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                return subprocess.CompletedProcess(
                    args, 0, stdout=f"{branch_name}\n", stderr=""
                )
            if args[:2] == ["git", "checkout"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:3] == ["git", "branch", "-D"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            # The leftover branch-auto-stash entry the test is asserting on.
            if args[:3] == ["git", "stash", "list"]:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=(
                        f"stash@{{0}}: On main: tusk-branch: auto-stash for TASK-{task_id}\n"
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_abandon, "run", _mock_run)
        # The hoisted helper lives on `_merge` and was bound onto tusk_abandon
        # at import time. Patch the merge module's `run` too so the helper
        # records its `git stash list` calls into `calls`.
        monkeypatch.setattr(tusk_abandon._merge, "run", _mock_run)

        rc, result, stderr = _call(
            db_path,
            config_path,
            task_id,
            "--reason",
            "wont_do",
            "--session",
            session_id,
        )

        assert rc == 0, f"abandon failed: {stderr}"
        assert result is not None, f"expected JSON on stdout; stderr was:\n{stderr}"
        assert result["task"]["status"] == "Done"

        # The stash is preserved and surfaced to the user.
        assert not any(c[:3] == ["git", "stash", "drop"] for c in calls), (
            f"expected no git stash drop call; got calls:\n{calls}"
        )
        assert "Warning: preserved tusk branch auto-stash" in stderr
        assert "git stash pop stash@{0}" in stderr
        assert "git stash drop stash@{0}" in stderr

    def test_abandon_silent_when_no_branch_auto_stash_present(
        self, db_path, config_path, monkeypatch
    ):
        """When no leftover branch-stash exists, abandon proceeds without any
        `git stash drop` call or warning — the helper is a no-op."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        monkeypatch.setattr(
            tusk_abandon,
            "find_task_branch",
            lambda tid: (None, f"No branch found matching feature/TASK-{tid}-*", False),
        )
        monkeypatch.setattr(tusk_abandon, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_abandon, "checkpoint_wal", lambda db: None)

        calls: list[list[str]] = []

        def _mock_run(args, check=True):
            calls.append(args)
            if args[:3] == ["git", "stash", "list"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_abandon, "run", _mock_run)
        monkeypatch.setattr(tusk_abandon._merge, "run", _mock_run)

        rc, _, stderr = _call(
            db_path,
            config_path,
            task_id,
            "--reason",
            "wont_do",
            "--session",
            session_id,
        )

        assert rc == 0, f"abandon failed: {stderr}"
        # The helper still ran (`git stash list` was queried), but no drop
        # was attempted because no matching entry existed.
        assert any(c[:3] == ["git", "stash", "list"] for c in calls)
        assert not any(c[:3] == ["git", "stash", "drop"] for c in calls)


class TestAbandonIdempotentRetry:
    """tusk abandon must be idempotent on retry when session/task are already closed (issue #808).

    Scenario: a prior abandon got past session-close + task-done but failed at
    worktree removal (e.g. dirty worktree). After the user cleans the worktree
    and reruns the same `tusk abandon` args, the session is already closed and
    the task is already Done. Before the fix, task-done refused with "Task X is
    already Done" and abandon exited 2 without finishing the cleanup.
    """

    def test_already_done_task_is_treated_as_success(
        self, db_path, config_path, monkeypatch
    ):
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn, status="Done")
            conn.execute(
                "UPDATE tasks SET closed_reason = 'completed', "
                "closed_at = datetime('now') WHERE id = ?",
                (task_id,),
            )
            session_id = _insert_session(conn, task_id, closed=True)
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setattr(
            tusk_abandon,
            "find_task_branch",
            lambda tid: (None, f"No branch found matching feature/TASK-{tid}-*", False),
        )
        monkeypatch.setattr(tusk_abandon, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_abandon, "checkpoint_wal", lambda db: None)

        rc, result, stderr = _call(
            db_path,
            config_path,
            task_id,
            "--reason",
            "completed",
            "--session",
            session_id,
        )

        assert rc == 0, f"abandon retry failed: {stderr}"
        assert result is not None, f"expected JSON on stdout; stderr was:\n{stderr}"
        assert result["task"]["status"] == "Done"
        assert result["task"]["closed_reason"] == "completed"
        assert "is already Done" in stderr
        # task-done should have been treated as a no-op rather than fatal.
        assert "Error: task-done failed" not in stderr

    def test_already_done_task_synthesizes_unblocked_tasks(
        self, db_path, config_path, monkeypatch
    ):
        """Synthesized result must include unblocked_tasks for any deps newly satisfied."""
        conn = sqlite3.connect(str(db_path))
        try:
            blocker_id = _insert_task(conn, status="Done")
            conn.execute(
                "UPDATE tasks SET closed_reason = 'completed', "
                "closed_at = datetime('now') WHERE id = ?",
                (blocker_id,),
            )
            dependent_id = _insert_task(conn, status="To Do")
            conn.execute(
                "INSERT INTO task_dependencies (task_id, depends_on_id, relationship_type) "
                "VALUES (?, ?, 'blocks')",
                (dependent_id, blocker_id),
            )
            session_id = _insert_session(conn, blocker_id, closed=True)
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setattr(
            tusk_abandon,
            "find_task_branch",
            lambda tid: (None, f"No branch found matching feature/TASK-{tid}-*", False),
        )
        monkeypatch.setattr(tusk_abandon, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_abandon, "checkpoint_wal", lambda db: None)

        rc, result, stderr = _call(
            db_path,
            config_path,
            blocker_id,
            "--reason",
            "completed",
            "--session",
            session_id,
        )

        assert rc == 0, f"abandon failed: {stderr}"
        unblocked_ids = [t["id"] for t in result["unblocked_tasks"]]
        assert dependent_id in unblocked_ids

    def test_abandon_note_is_not_duplicated_on_retry(
        self, db_path, config_path, monkeypatch
    ):
        """Second abandon with the same --note must not pile up duplicate task_progress rows."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        monkeypatch.setattr(
            tusk_abandon,
            "find_task_branch",
            lambda tid: (None, f"No branch found matching feature/TASK-{tid}-*", False),
        )
        monkeypatch.setattr(tusk_abandon, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_abandon, "checkpoint_wal", lambda db: None)

        note = "Worktree had dirty changes; retried after cleaning."
        rc, _, stderr = _call(
            db_path,
            config_path,
            task_id,
            "--reason",
            "completed",
            "--session",
            session_id,
            "--note",
            note,
        )
        assert rc == 0, f"first abandon failed: {stderr}"

        conn = sqlite3.connect(str(db_path))
        try:
            count_after_first = conn.execute(
                "SELECT COUNT(*) FROM task_progress WHERE task_id = ? "
                "AND commit_message LIKE '[abandon:%'",
                (task_id,),
            ).fetchone()[0]
        finally:
            conn.close()

        # Simulated retry: task is already Done, session already closed, but
        # the user reruns the same args because the original worktree-remove
        # leg failed.
        rc2, _, stderr2 = _call(
            db_path,
            config_path,
            task_id,
            "--reason",
            "completed",
            "--session",
            session_id,
            "--note",
            note,
        )
        assert rc2 == 0, f"retry abandon failed: {stderr2}"

        conn = sqlite3.connect(str(db_path))
        try:
            count_after_retry = conn.execute(
                "SELECT COUNT(*) FROM task_progress WHERE task_id = ? "
                "AND commit_message LIKE '[abandon:%'",
                (task_id,),
            ).fetchone()[0]
        finally:
            conn.close()

        assert count_after_retry == count_after_first, (
            f"expected idempotent insert; first run wrote {count_after_first} note(s), "
            f"retry wrote {count_after_retry - count_after_first} additional row(s)"
        )


class TestAbandonNoSession:
    """Issue #829: abandon's three reasons (completed/duplicate/wont_do) all
    mean 'no session-tied work happened'. When a task has zero sessions and
    no feature branch, abandon must proceed without a session-close rather
    than refuse — the branch-safety check still guards against losing
    committed work."""

    @pytest.mark.parametrize("reason", ["wont_do", "duplicate", "completed"])
    def test_abandon_no_session_succeeds(
        self, db_path, config_path, monkeypatch, reason
    ):
        """Task with zero sessions + no feature branch → abandon exits 0,
        task marked Done with the given closed_reason, no session-close
        invoked, sessions_closed counter is 0."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            # No session inserted — this is the issue #829 case.
        finally:
            conn.close()

        # No feature branch and no recorded workspace.
        monkeypatch.setattr(
            tusk_abandon,
            "find_task_branch",
            lambda tid: (None, f"No branch found matching feature/TASK-{tid}-*", False),
        )
        monkeypatch.setattr(tusk_abandon, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_abandon, "checkpoint_wal", lambda db: None)

        rc, result, stderr = _call(
            db_path,
            config_path,
            task_id,
            "--reason",
            reason,
        )

        assert rc == 0, f"abandon failed: {stderr}"
        assert result is not None, f"expected JSON on stdout; stderr was:\n{stderr}"
        assert result["task"]["status"] == "Done"
        assert result["task"]["closed_reason"] == reason
        # No session existed, so nothing was closed.
        assert result["sessions_closed"] == 0

        # The escape-hatch Note line should be on stderr so operators can
        # see why session-close was skipped.
        assert "No session found for task" in stderr
        assert f"reason: {reason}" in stderr

        # Sanity-check: no session row was synthesized as a side effect.
        conn = sqlite3.connect(str(db_path))
        try:
            session_count = conn.execute(
                "SELECT COUNT(*) FROM task_sessions WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
            assert session_count == 0, (
                f"abandon's no-session path must not create a session row "
                f"(got {session_count})"
            )
        finally:
            conn.close()

    def test_abandon_no_session_records_note(
        self, db_path, config_path, monkeypatch
    ):
        """--note still lands on task_progress even when no session exists —
        the rationale must survive whether or not a session was attached."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
        finally:
            conn.close()

        monkeypatch.setattr(
            tusk_abandon,
            "find_task_branch",
            lambda tid: (None, f"No branch found matching feature/TASK-{tid}-*", False),
        )
        monkeypatch.setattr(tusk_abandon, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_abandon, "checkpoint_wal", lambda db: None)

        rc, _, stderr = _call(
            db_path,
            config_path,
            task_id,
            "--reason",
            "completed",
            "--note",
            "Subsumed by TASK-999 commit abc1234.",
        )

        assert rc == 0, f"abandon failed: {stderr}"

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT commit_message FROM task_progress WHERE task_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            assert row is not None, "expected a task_progress row for the note"
            assert "[abandon: completed]" in row[0]
            assert "Subsumed by TASK-999" in row[0]
        finally:
            conn.close()

    def test_abandon_with_session_unaffected_by_no_session_escape_hatch(
        self, db_path, config_path, monkeypatch
    ):
        """Regression guard: when a session DOES exist, abandon must take
        the normal autodetect/session-close path. The no-session escape
        hatch from issue #829 must not short-circuit this case."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
            session_id = _insert_session(conn, task_id)
        finally:
            conn.close()

        monkeypatch.setattr(
            tusk_abandon,
            "find_task_branch",
            lambda tid: (None, f"No branch found matching feature/TASK-{tid}-*", False),
        )
        monkeypatch.setattr(tusk_abandon, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_abandon, "checkpoint_wal", lambda db: None)

        # Do NOT pass --session — exercise autodetect's existing path with
        # an open session present.
        rc, result, stderr = _call(
            db_path, config_path, task_id, "--reason", "completed"
        )

        assert rc == 0, f"abandon failed: {stderr}"
        assert result["sessions_closed"] == 1, (
            "abandon with an existing open session must still close it"
        )
        # The no-session Note must NOT appear when a session exists.
        assert "No session found for task" not in stderr

        # Verify the session is actually closed in the DB.
        conn = sqlite3.connect(str(db_path))
        try:
            ended = conn.execute(
                "SELECT ended_at FROM task_sessions WHERE id = ?", (session_id,)
            ).fetchone()[0]
            assert ended is not None, "open session should have been closed"
        finally:
            conn.close()
