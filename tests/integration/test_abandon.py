"""Integration tests for `tusk abandon` (TASK-49).

`tusk abandon` is the no-commit symmetric of `tusk merge`: it closes a task
with closed_reason in (wont_do, duplicate), force-deletes the feature branch
when one exists, closes the open session, and emits JSON in the same shape
as `tusk merge`.

Exercises:
  - Both wont_do and duplicate reasons (the only two reasons abandon accepts)
    correctly close the task and the session.
  - Refuses (exit code 2) when the feature branch has commits not on the
    default branch, with an error pointing the user at `tusk merge`.
  - Rejects reasons that aren't in the abandon set (e.g. `completed`).
  - Optional `--note` is persisted to task_progress so the rationale survives.
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
    """Both abandon reasons close the task and the open session in one call."""

    @pytest.mark.parametrize("reason", ["wont_do", "duplicate"])
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
                    args, 0, stdout="abc1234 some unmerged work\n", stderr=""
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

    @pytest.mark.parametrize("bad_reason", ["completed", "expired", "garbage"])
    def test_rejects_non_abandon_reasons(self, db_path, config_path, bad_reason):
        rc, result, stderr = _call(
            db_path, config_path, 1, "--reason", bad_reason
        )
        assert rc == 1
        assert result is None
        assert "wont_do|duplicate" in stderr

    def test_rejects_missing_reason(self, db_path, config_path):
        rc, _, stderr = _call(db_path, config_path, 1)
        assert rc == 1
        assert "--reason" in stderr


class TestAbandonDropsBranchAutoStash:
    """Issue #647 — sibling of #644.

    `tusk branch <id>` auto-stashes a dirty working tree under
    `tusk-branch: auto-stash for TASK-<id>` so it can drop the user back on
    the default branch cleanly. That stash cannot belong to the task being
    started (no work has happened yet at branch time), so it is by definition
    unrelated leftover state. `tusk merge` drops it on a successful ship
    (TASK-290); `tusk abandon` must do the same when a task is closed without
    shipping, otherwise the orphan accumulates in `git stash list` forever
    for `wont_do` / `duplicate` closures — exactly the cases where a stash
    is least likely to be remembered later.
    """

    def test_abandon_drops_branch_auto_stash_after_branch_delete(
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
        # the test DB. Order matters — we assert stash drop lands AFTER
        # `git branch -D`.
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
            if args[:3] == ["git", "stash", "drop"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_abandon, "run", _mock_run)
        # The hoisted helper lives on `_merge` and was bound onto tusk_abandon
        # at import time. Patch the merge module's `run` too so the helper
        # records its `git stash list` / `git stash drop` calls into `calls`.
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

        # The stash was dropped.
        assert ["git", "stash", "drop", "stash@{0}"] in calls, (
            f"expected git stash drop call; got calls:\n{calls}"
        )

        # Ordering: branch -D must precede stash drop. The stash drop runs as
        # part of the abandon flow before task-done returns, and task-done
        # marks the task Done (asserted above), so the drop precedes closure.
        delete_idx = next(
            i for i, c in enumerate(calls) if c[:3] == ["git", "branch", "-D"]
        )
        drop_idx = next(
            i for i, c in enumerate(calls) if c[:3] == ["git", "stash", "drop"]
        )
        assert delete_idx < drop_idx, (
            f"stash drop must run after branch delete; "
            f"delete at {delete_idx}, drop at {drop_idx}"
        )

    def test_abandon_silent_when_no_branch_auto_stash_present(
        self, db_path, config_path, monkeypatch
    ):
        """When no leftover branch-stash exists, abandon proceeds without any
        `git stash drop` call — the helper is a no-op."""
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
