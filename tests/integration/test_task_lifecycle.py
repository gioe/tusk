"""Integration tests for tusk task lifecycle (start, done, criteria guards).

Tests the full lifecycle: insert a task, start it, complete criteria, close it.
Guard cases: closing with open criteria returns exit code 3 without --force;
closing with --force succeeds; wont_do closed_reason skips the commit-hash check;
already-Done task returns exit code 2. Invalid status transitions are rejected
by the DB trigger.
"""

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr, redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        os.path.join(REPO_ROOT, "bin", f"{name}.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_task_start = _load("tusk-task-start")
tusk_task_done = _load("tusk-task-done")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def insert_task(
    conn: sqlite3.Connection,
    summary: str,
    *,
    status: str = "To Do",
    closed_reason: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO tasks (summary, status, closed_reason, task_type, priority, complexity, priority_score)"
        " VALUES (?, ?, ?, 'feature', 'Medium', 'S', 50)",
        (summary, status, closed_reason),
    )
    conn.commit()
    return cur.lastrowid


def insert_criterion(
    conn: sqlite3.Connection,
    task_id: int,
    text: str,
    *,
    is_completed: int = 0,
    commit_hash: str | None = None,
    criterion_type: str = "manual",
) -> int:
    cur = conn.execute(
        "INSERT INTO acceptance_criteria (task_id, criterion, source, is_completed, commit_hash, criterion_type)"
        " VALUES (?, ?, 'original', ?, ?, ?)",
        (task_id, text, is_completed, commit_hash, criterion_type),
    )
    conn.commit()
    return cur.lastrowid


def call_start(db_path, config_path, task_id, *extra_args) -> tuple[int, dict | None, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_task_start.main([str(db_path), str(config_path), str(task_id), *extra_args])
    out = out_buf.getvalue().strip()
    result = json.loads(out) if out else None
    return rc, result, err_buf.getvalue()


def call_done(db_path, config_path, task_id, reason, *extra_args) -> tuple[int, dict | None, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_task_done.main(
            [str(db_path), str(config_path), str(task_id), "--reason", reason, *extra_args]
        )
    out = out_buf.getvalue().strip()
    result = json.loads(out) if out else None
    return rc, result, err_buf.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTaskLifecycle:

    def test_happy_path_full_lifecycle(self, db_path, config_path):
        """CID 1524: insert -> start -> criteria done -> close succeeds end-to-end."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Happy path task")
            cid = insert_criterion(conn, task_id, "Write the feature")
        finally:
            conn.close()

        # Start the task
        rc, result, _ = call_start(db_path, config_path, task_id)
        assert rc == 0
        assert result["task"]["status"] == "In Progress"
        assert result["session_id"] is not None

        # Mark criterion done with a commit hash
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute(
                "UPDATE acceptance_criteria SET is_completed = 1, commit_hash = 'abc1234'"
                " WHERE id = ?",
                (cid,),
            )
            conn.commit()
        finally:
            conn.close()

        # Close the task
        rc, result, _ = call_done(db_path, config_path, task_id, "completed")
        assert rc == 0
        assert result["task"]["status"] == "Done"
        assert result["task"]["closed_reason"] == "completed"

    def test_open_criteria_blocks_closure_exit_code_3(self, db_path, config_path, tmp_path, monkeypatch):

        """CID 1525: closing with open criteria returns exit code 3 and stderr message when no task commits exist."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Task with open criterion")
            insert_criterion(conn, task_id, "Still pending criterion")
            # Manually set to In Progress to match real workflow state for the guard test
            conn.execute(
                "UPDATE tasks SET status = 'In Progress' WHERE id = ?", (task_id,)
            )
            conn.commit()
        finally:
            conn.close()

        # Run from a directory with no git history so _find_task_commits returns []
        monkeypatch.chdir(tmp_path)

        rc, result, stderr = call_done(db_path, config_path, task_id, "completed")

        assert rc == 3
        assert result is None
        assert "not yet marked done" in stderr
        assert "--force" in stderr

    def test_open_criteria_error_lists_only_uncompleted_ids(self, db_path, config_path, tmp_path, monkeypatch):
        """Regression for GitHub Issue #463: the task-done open-criteria error must
        list only the criteria still open — not any criteria already marked done —
        and must surface the X/Y done count so the two sets are unambiguous."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Task with mix of done and open criteria")
            done_cid_1 = insert_criterion(conn, task_id, "Done one", is_completed=1, commit_hash="deadbee")
            done_cid_2 = insert_criterion(conn, task_id, "Done two", is_completed=1, commit_hash="deadbee")
            open_cid_1 = insert_criterion(conn, task_id, "Open one")
            open_cid_2 = insert_criterion(conn, task_id, "Open two")
            conn.execute(
                "UPDATE tasks SET status = 'In Progress' WHERE id = ?", (task_id,)
            )
            conn.commit()
        finally:
            conn.close()

        monkeypatch.chdir(tmp_path)

        rc, result, stderr = call_done(db_path, config_path, task_id, "completed")

        assert rc == 3
        assert result is None
        # Error must reference the open IDs and their criterion text
        assert f"[{open_cid_1}] Open one" in stderr
        assert f"[{open_cid_2}] Open two" in stderr
        # Must NOT reference the already-completed criteria anywhere in the error
        assert f"[{done_cid_1}]" not in stderr
        assert f"[{done_cid_2}]" not in stderr
        assert "Done one" not in stderr
        assert "Done two" not in stderr
        # X/Y count makes the disambiguation explicit
        assert "2/4 criteria done" in stderr
        assert "2 not yet marked done" in stderr

    def test_force_flag_closes_task_with_open_criteria(self, db_path, config_path):
        """CID 1526: --force closes task even with open criteria."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Force close task")
            insert_criterion(conn, task_id, "Open criterion")
            conn.execute(
                "UPDATE tasks SET status = 'In Progress' WHERE id = ?", (task_id,)
            )
            conn.commit()
        finally:
            conn.close()

        rc, result, _ = call_done(db_path, config_path, task_id, "completed", "--force")

        assert rc == 0
        assert result["task"]["status"] == "Done"
        assert result["task"]["closed_reason"] == "completed"

    def test_wont_do_skips_commit_hash_check(self, db_path, config_path):
        """CID 1527: wont_do closure succeeds even when completed criteria lack a commit hash."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Wont do task")
            # Criterion is completed but has no commit_hash — would block 'completed' reason
            insert_criterion(conn, task_id, "Done but uncommitted", is_completed=1, commit_hash=None)
            conn.execute(
                "UPDATE tasks SET status = 'In Progress' WHERE id = ?", (task_id,)
            )
            conn.commit()
        finally:
            conn.close()

        rc, result, _ = call_done(db_path, config_path, task_id, "wont_do")

        assert rc == 0
        assert result["task"]["closed_reason"] == "wont_do"

    def test_completed_reason_blocked_when_criterion_lacks_commit_hash(self, db_path, config_path):
        """CID 1535: completed closure is blocked (exit code 3) when a completed typed criterion has no commit_hash.

        Manual criteria are excluded from this check (Issue #609) — a code/test/file
        criterion is required to exercise the block.
        """
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Completed no-hash task")
            # Typed criterion (code) is completed but has no commit_hash — this is the
            # case the check is designed to flag. Manual criteria are excluded entirely.
            insert_criterion(
                conn, task_id, "Done but uncommitted",
                is_completed=1, commit_hash=None, criterion_type="code",
            )
            conn.execute(
                "UPDATE tasks SET status = 'In Progress' WHERE id = ?", (task_id,)
            )
            conn.commit()
        finally:
            conn.close()

        rc, result, stderr = call_done(db_path, config_path, task_id, "completed")

        assert rc == 3
        assert result is None
        assert "commit_hash" in stderr or "commit" in stderr.lower()

    def test_manual_criterion_without_commit_hash_does_not_block_completed(
        self, db_path, config_path
    ):
        """Issue #609: a completed manual criterion with no commit_hash must NOT
        block 'completed' closure — manual criteria carry no code by definition,
        so binding them to a commit hash is conceptually wrong. The query in
        bin/tusk-task-done.py excludes criterion_type='manual'.
        """
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Verification-only manual task")
            insert_criterion(
                conn, task_id, "Manual verification step",
                is_completed=1, commit_hash=None, criterion_type="manual",
            )
            conn.execute(
                "UPDATE tasks SET status = 'In Progress' WHERE id = ?", (task_id,)
            )
            conn.commit()
        finally:
            conn.close()

        rc, result, stderr = call_done(db_path, config_path, task_id, "completed")

        assert rc == 0, f"Expected exit 0, got {rc}\nstderr: {stderr}"
        assert result["task"]["status"] == "Done"
        assert result["task"]["closed_reason"] == "completed"
        # No diagnostic about missing commit hashes — the manual criterion is excluded.
        assert "criteria without a commit hash" not in stderr

    def test_manual_criterion_does_not_warn_even_with_force(
        self, db_path, config_path
    ):
        """Issue #609: --force must also produce no 'criteria without a commit hash'
        diagnostic when the only completed-but-uncommitted criteria are manual.
        Mirrors the merge auto-complete / normal path which always passes --force.
        """
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Force close manual-only task")
            insert_criterion(
                conn, task_id, "Manual one",
                is_completed=1, commit_hash=None, criterion_type="manual",
            )
            insert_criterion(
                conn, task_id, "Manual two",
                is_completed=1, commit_hash=None, criterion_type="manual",
            )
            conn.execute(
                "UPDATE tasks SET status = 'In Progress' WHERE id = ?", (task_id,)
            )
            conn.commit()
        finally:
            conn.close()

        rc, result, stderr = call_done(
            db_path, config_path, task_id, "completed", "--force"
        )

        assert rc == 0
        assert result["task"]["status"] == "Done"
        assert "criteria without a commit hash" not in stderr

    def test_typed_criterion_without_commit_hash_with_force_still_warns(
        self, db_path, config_path
    ):
        """Issue #609 regression guard: the manual-exclusion change must not
        silence the warning for typed criteria — a code/test/file criterion that
        legitimately lacks a commit hash should still emit the 'Warning:' diagnostic
        on --force closure so the audit trail survives.
        """
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Force close typed-uncommitted task")
            insert_criterion(
                conn, task_id, "Typed but uncommitted",
                is_completed=1, commit_hash=None, criterion_type="code",
            )
            conn.execute(
                "UPDATE tasks SET status = 'In Progress' WHERE id = ?", (task_id,)
            )
            conn.commit()
        finally:
            conn.close()

        rc, result, stderr = call_done(
            db_path, config_path, task_id, "completed", "--force"
        )

        assert rc == 0
        assert result["task"]["status"] == "Done"
        # Warning still printed for typed criteria — diagnostic preserved.
        assert "Warning" in stderr
        assert "criteria without a commit hash" in stderr

    def test_already_done_task_returns_exit_code_2(self, db_path, config_path):
        """CID 1528: calling task-done on an already-Done task returns exit code 2."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Already done task", status="Done", closed_reason="completed")
        finally:
            conn.close()

        rc, result, stderr = call_done(db_path, config_path, task_id, "completed")

        assert rc == 2
        assert result is None
        assert "already Done" in stderr

    def test_task_done_usage_enumerates_reason_values(self, db_path, config_path):
        """CID 1646: task-done usage string shows valid --reason values, not a generic placeholder."""
        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            with pytest.raises(SystemExit):
                tusk_task_done.main([str(db_path), str(config_path)])
        usage = err_buf.getvalue()
        assert "completed" in usage
        assert "expired" in usage
        assert "wont_do" in usage
        assert "duplicate" in usage
        assert "<closed_reason>" not in usage

    def test_auto_marks_criteria_done_when_task_commits_found(self, db_path, config_path, tmp_path, monkeypatch):
        """CID 1654: open criteria are auto-marked done (with commit hash) when [TASK-N] commits exist."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Task with commits in git log")
            cid = insert_criterion(conn, task_id, "Implement the feature")
            conn.execute(
                "UPDATE tasks SET status = 'In Progress' WHERE id = ?", (task_id,)
            )
            conn.commit()
        finally:
            conn.close()

        # Set up a temp git repo with a commit referencing this task
        git_dir = tmp_path / "git_repo"
        git_dir.mkdir()
        subprocess.run(["git", "init"], cwd=git_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=git_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=git_dir, check=True, capture_output=True)
        (git_dir / "file.txt").write_text("work done")
        subprocess.run(["git", "add", "."], cwd=git_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"[TASK-{task_id}] Implement the feature"],
            cwd=git_dir, check=True, capture_output=True,
        )

        monkeypatch.chdir(git_dir)
        rc, result, _ = call_done(db_path, config_path, task_id, "completed")

        assert rc == 0
        assert result["task"]["status"] == "Done"

        # Verify criterion was auto-marked done with the commit hash attached
        conn = sqlite3.connect(str(db_path))
        try:
            crit = conn.execute(
                "SELECT is_completed, commit_hash FROM acceptance_criteria WHERE id = ?", (cid,)
            ).fetchone()
            assert crit[0] == 1, "criterion should be completed"
            assert crit[1] is not None, "commit_hash should be populated"
        finally:
            conn.close()

    def test_wont_do_does_not_auto_mark_criteria_even_with_commits(self, db_path, config_path, tmp_path, monkeypatch):
        """CID 1653: auto-marking only fires for reason='completed'; wont_do leaves open criteria untouched."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Wont-do task with commits")
            cid = insert_criterion(conn, task_id, "Never completed criterion")
            conn.execute(
                "UPDATE tasks SET status = 'In Progress' WHERE id = ?", (task_id,)
            )
            conn.commit()
        finally:
            conn.close()

        # Set up a git repo with a matching commit — should NOT trigger auto-mark for wont_do
        git_dir = tmp_path / "git_repo"
        git_dir.mkdir()
        subprocess.run(["git", "init"], cwd=git_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=git_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=git_dir, check=True, capture_output=True)
        (git_dir / "file.txt").write_text("partial work")
        subprocess.run(["git", "add", "."], cwd=git_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"[TASK-{task_id}] Partial work"],
            cwd=git_dir, check=True, capture_output=True,
        )

        monkeypatch.chdir(git_dir)
        rc, result, stderr = call_done(db_path, config_path, task_id, "wont_do")

        # wont_do is still blocked by open criteria (no auto-mark) — use --force to override
        assert rc == 3
        assert "not yet marked done" in stderr

        # Criterion must remain uncompleted — auto-mark never fires for wont_do
        conn = sqlite3.connect(str(db_path))
        try:
            crit = conn.execute(
                "SELECT is_completed FROM acceptance_criteria WHERE id = ?", (cid,)
            ).fetchone()
            assert crit[0] == 0, "criterion should remain incomplete for wont_do closure"
        finally:
            conn.close()

    def test_invalid_status_transition_rejected_by_trigger(self, db_path, config_path):
        """CID 1529: DB trigger blocks invalid transitions (e.g. In Progress -> To Do)."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Trigger test task")
            insert_criterion(conn, task_id, "Some criterion")
            # Advance to In Progress (valid transition)
            conn.execute(
                "UPDATE tasks SET status = 'In Progress' WHERE id = ?", (task_id,)
            )
            conn.commit()

            # Attempt invalid transition: In Progress -> To Do
            with pytest.raises(sqlite3.IntegrityError, match="Invalid status transition"):
                conn.execute(
                    "UPDATE tasks SET status = 'To Do' WHERE id = ?", (task_id,)
                )
        finally:
            conn.close()

    def test_invalid_status_transition_error_names_escape_hatches(self, db_path, config_path):
        """Issue #616: error message must point users at task-reopen / task-unstart."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Escape-hatch hint test")
            insert_criterion(conn, task_id, "Some criterion")
            conn.execute(
                "UPDATE tasks SET status = 'In Progress' WHERE id = ?", (task_id,)
            )
            conn.commit()

            with pytest.raises(sqlite3.IntegrityError) as exc_info:
                conn.execute(
                    "UPDATE tasks SET status = 'To Do' WHERE id = ?", (task_id,)
                )
            msg = str(exc_info.value)
            assert "tusk task-reopen <id> --force" in msg
            assert "tusk task-unstart <id> --force" in msg
        finally:
            conn.close()

    def test_task_start_warns_when_referenced_task_is_todo(self, db_path, config_path):
        """CID 172/173: task-start emits warning to stderr when description references a To Do task."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            prereq_id = insert_task(conn, "Prerequisite task")
            insert_criterion(conn, prereq_id, "Prereq criterion")
            main_id = conn.execute(
                "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score, description)"
                " VALUES (?, 'To Do', 'feature', 'Medium', 'S', 80, ?)",
                (
                    "Main task",
                    f"This task requires TASK-{prereq_id} to be completed first.",
                ),
            ).lastrowid
            conn.commit()
            insert_criterion(conn, main_id, "Main criterion")
        finally:
            conn.close()

        rc, result, stderr = call_start(db_path, config_path, main_id)

        assert rc == 0
        assert result is not None
        assert result["task"]["summary"] == "Main task"
        assert "Warning" in stderr
        assert f"TASK-{prereq_id}" in stderr
        assert "Prerequisite task" in stderr

    def test_task_start_no_warning_when_referenced_task_is_in_progress(self, db_path, config_path):
        """CID 174: no warning when the referenced task is In Progress (not To Do)."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            prereq_id = insert_task(conn, "In-progress prereq", status="In Progress")
            insert_criterion(conn, prereq_id, "Prereq criterion")
            main_id = conn.execute(
                "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score, description)"
                " VALUES (?, 'To Do', 'feature', 'Medium', 'S', 80, ?)",
                ("Main task", f"Depends on TASK-{prereq_id}."),
            ).lastrowid
            conn.commit()
            insert_criterion(conn, main_id, "Main criterion")
        finally:
            conn.close()

        rc, result, stderr = call_start(db_path, config_path, main_id)

        assert rc == 0
        assert result is not None
        assert "Warning" not in stderr

    def test_task_start_no_warning_when_no_task_references(self, db_path, config_path):
        """CID 174: no warning when the task has no TASK-NNN references."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            main_id = insert_task(conn, "Standalone task")
            insert_criterion(conn, main_id, "Some criterion")
        finally:
            conn.close()

        rc, result, stderr = call_start(db_path, config_path, main_id)

        assert rc == 0
        assert result is not None
        assert "Warning" not in stderr
