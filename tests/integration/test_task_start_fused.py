"""Integration tests for the fused tusk-task-start no-ID path.

When invoked without a task_id argument, tusk-task-start picks the top
WSJF-ranked ready task from v_ready_tasks and starts it in one call —
eliminating the select+start round-trip the /tusk no-arg path used to pay.
These tests lock the fused-path behavior (top pick, empty-backlog exit,
blocked-task skipping, prerequisite-warning preservation) and verify that
the explicit <task_id> path is unchanged.
"""

import importlib.util
import io
import json
import os
import sqlite3
from contextlib import redirect_stderr, redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_task_start",
    os.path.join(REPO_ROOT, "bin", "tusk-task-start.py"),
)
tusk_task_start = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tusk_task_start)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def insert_task(
    conn: sqlite3.Connection,
    summary: str,
    *,
    status: str = "To Do",
    priority_score: int = 60,
    complexity: str = "S",
    description: str = "",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO tasks (summary, status, priority, complexity, task_type,
                           priority_score, description)
        VALUES (?, ?, 'Medium', ?, 'feature', ?, ?)
        """,
        (summary, status, complexity, priority_score, description),
    )
    conn.commit()
    return cur.lastrowid


def insert_criterion(conn: sqlite3.Connection, task_id: int, text: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO acceptance_criteria (task_id, criterion, source, is_completed)
        VALUES (?, ?, 'original', 0)
        """,
        (task_id, text),
    )
    conn.commit()
    return cur.lastrowid


def add_blocking_dep(conn: sqlite3.Connection, task_id: int, depends_on_id: int) -> None:
    conn.execute(
        """
        INSERT INTO task_dependencies (task_id, depends_on_id, relationship_type)
        VALUES (?, ?, 'blocks')
        """,
        (task_id, depends_on_id),
    )
    conn.commit()


def call_start(db_path, config_path, *extra_args) -> tuple[int, dict | None, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_task_start.main([str(db_path), str(config_path), *extra_args])
    out = out_buf.getvalue().strip()
    result = json.loads(out) if out else None
    return rc, result, err_buf.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFusedTaskStart:
    def test_no_id_starts_top_wsjf_ready_task(self, db_path, config_path):
        """CID 425: task-start with no ID picks the top WSJF-ranked ready task and starts it."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            low_id = insert_task(conn, "Low priority task", priority_score=20)
            insert_criterion(conn, low_id, "do the low thing")
            high_id = insert_task(conn, "High priority task", priority_score=90)
            insert_criterion(conn, high_id, "do the high thing")
            mid_id = insert_task(conn, "Medium priority task", priority_score=50)
            insert_criterion(conn, mid_id, "do the mid thing")
        finally:
            conn.close()

        rc, result, _ = call_start(db_path, config_path, "--force")

        assert rc == 0
        assert result is not None
        assert result["task"]["id"] == high_id
        assert result["task"]["summary"] == "High priority task"
        assert result["task"]["status"] == "In Progress"
        assert result["session_id"] is not None

    def test_no_id_exits_1_when_backlog_empty(self, db_path, config_path):
        """CID 429: exit 1 with the canonical empty-backlog stderr when no ready tasks exist."""
        rc, result, stderr = call_start(db_path, config_path, "--force")

        assert rc == 1
        assert result is None
        assert "No ready tasks found" in stderr

    def test_no_id_skips_blocked_tasks(self, db_path, config_path):
        """CID 425: fused path respects v_ready_tasks — a blocked high-priority task is skipped."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            blocker_id = insert_task(conn, "Blocker task", priority_score=40)
            insert_criterion(conn, blocker_id, "unblock")
            blocked_id = insert_task(conn, "Blocked high-priority task", priority_score=100)
            insert_criterion(conn, blocked_id, "do the blocked thing")
            add_blocking_dep(conn, blocked_id, blocker_id)
        finally:
            conn.close()

        rc, result, _ = call_start(db_path, config_path, "--force")

        assert rc == 0
        assert result is not None
        assert result["task"]["id"] == blocker_id
        assert result["task"]["status"] == "In Progress"

    def test_no_id_preserves_prerequisite_warning(self, db_path, config_path):
        """CID 428: the 'references unfinished prerequisite tasks' stderr warning
        still fires when the selected-via-fused-path task references a To Do TASK-NNN."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            prereq_id = insert_task(conn, "Prerequisite task", priority_score=30)
            insert_criterion(conn, prereq_id, "do the prereq")
            main_id = insert_task(
                conn,
                "Main task",
                priority_score=100,
                description=f"Depends on TASK-{prereq_id} being finished first.",
            )
            insert_criterion(conn, main_id, "do the main thing")
        finally:
            conn.close()

        rc, result, stderr = call_start(db_path, config_path, "--force")

        assert rc == 0
        assert result is not None
        assert result["task"]["id"] == main_id
        assert "Warning" in stderr
        assert f"TASK-{prereq_id}" in stderr
        assert "Prerequisite task" in stderr

    def test_skill_flag_opens_skill_run_row(self, db_path, config_path):
        """--skill <name> opens a skill_runs row attributed to the started task
        and includes its details under result['skill_run']. Saves the follow-up
        `tusk skill-run start <name> --task-id <id>` call."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Task with fused skill-run", priority_score=60)
            insert_criterion(conn, task_id, "do the thing")
        finally:
            conn.close()

        rc, result, _ = call_start(
            db_path, config_path, str(task_id), "--force", "--skill", "tusk"
        )

        assert rc == 0
        assert result is not None
        assert result["task"]["id"] == task_id
        sr = result["skill_run"]
        assert sr is not None
        assert sr["skill_name"] == "tusk"
        assert sr["task_id"] == task_id
        assert isinstance(sr["run_id"], int) and sr["run_id"] > 0
        assert sr["started_at"]

        # Verify the row actually landed with the expected shape.
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT skill_name, task_id, ended_at FROM skill_runs WHERE id = ?",
                (sr["run_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "tusk"
        assert row[1] == task_id
        assert row[2] is None  # still open

    def test_without_skill_flag_skill_run_is_null(self, db_path, config_path):
        """Omitting --skill leaves result['skill_run'] = None and does not touch
        the skill_runs table — preserves the existing two-call pattern for
        callers that haven't migrated yet."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Task without skill flag", priority_score=60)
            insert_criterion(conn, task_id, "do the thing")
            before = conn.execute("SELECT COUNT(*) FROM skill_runs").fetchone()[0]
        finally:
            conn.close()

        rc, result, _ = call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 0
        assert result is not None
        assert result["skill_run"] is None

        conn = sqlite3.connect(str(db_path))
        try:
            after = conn.execute("SELECT COUNT(*) FROM skill_runs").fetchone()[0]
        finally:
            conn.close()
        assert after == before

    def test_skill_flag_not_inserted_on_error_exits(self, db_path, config_path):
        """Guard-rail exits (task not found / no criteria without --force) return
        before the skill_runs INSERT — critical for the early-exit-cleanup notes
        in SKILL.md that promise no orphaned skill_runs rows on pre-start exits."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            before = conn.execute("SELECT COUNT(*) FROM skill_runs").fetchone()[0]
            # Task exists but has zero criteria and --force is NOT passed → exit 2.
            no_crit_id = insert_task(conn, "Task without criteria", priority_score=60)
        finally:
            conn.close()

        rc, _, stderr = call_start(
            db_path, config_path, str(no_crit_id), "--skill", "tusk"
        )
        assert rc == 2
        assert "no acceptance criteria" in stderr

        conn = sqlite3.connect(str(db_path))
        try:
            after = conn.execute("SELECT COUNT(*) FROM skill_runs").fetchone()[0]
        finally:
            conn.close()
        assert after == before

    def test_reopened_task_with_legacy_null_criterion_starts_without_warning(
        self, db_path, config_path
    ):
        """A reopened task's legacy NULL defer flag still represents active criteria."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            # task-reopen returns a previously closed task to this To Do state
            # without rewriting its acceptance criteria.
            task_id = insert_task(conn, "Reopened task with legacy criterion")
            criterion_id = insert_criterion(conn, task_id, "preserve the criterion")
            conn.execute(
                "UPDATE acceptance_criteria SET is_deferred = NULL WHERE id = ?",
                (criterion_id,),
            )
            conn.commit()
        finally:
            conn.close()

        rc, result, stderr = call_start(db_path, config_path, str(task_id))

        assert rc == 0
        assert result is not None
        assert [row["id"] for row in result["criteria"]] == [criterion_id]
        assert "no acceptance criteria" not in stderr

    def test_task_with_only_deferred_criteria_still_fails_guard(
        self, db_path, config_path
    ):
        """Explicitly deferred criteria remain excluded from the active count."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Task with deferred criterion")
            criterion_id = insert_criterion(conn, task_id, "deferred work")
            conn.execute(
                "UPDATE acceptance_criteria SET is_deferred = 1 WHERE id = ?",
                (criterion_id,),
            )
            conn.commit()
        finally:
            conn.close()

        rc, result, stderr = call_start(db_path, config_path, str(task_id))

        assert rc == 2
        assert result is None
        assert "no acceptance criteria" in stderr

    def test_active_session_error_surfaces_abandoned_committed_work(
        self, db_path, config_path, monkeypatch
    ):
        """A stale open session with a finished skill-run and unmerged task commits
        should identify the likely-abandoned handoff state before --force-session."""
        monkeypatch.setattr(tusk_task_start, "_current_repo_root", lambda: "/repo")
        monkeypatch.setattr(
            tusk_task_start,
            "_unmerged_task_commits",
            lambda task_id, repo_root: ["abc1234"] if task_id else [],
            raising=False,
        )
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Abandoned session task", status="In Progress")
            insert_criterion(conn, task_id, "do the thing")
            cur = conn.execute(
                "INSERT INTO task_sessions (task_id, started_at) "
                "VALUES (?, datetime('now'))",
                (task_id,),
            )
            session_id = cur.lastrowid
            conn.execute(
                "INSERT INTO skill_runs (skill_name, task_id, ended_at) "
                "VALUES ('tusk', ?, datetime('now'))",
                (task_id,),
            )
            conn.execute(
                "INSERT INTO task_workspaces (task_id, branch, workspace_path) "
                "VALUES (?, ?, ?)",
                (task_id, f"feature/TASK-{task_id}-abandoned", "/workspace"),
            )
            conn.commit()
        finally:
            conn.close()

        rc, result, stderr = call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 2
        assert result is None
        assert f"session {session_id}" in stderr
        assert "likely abandoned" in stderr
        assert "skill-run is finished" in stderr
        assert "unmerged commits" in stderr
        assert "--force-session" in stderr

    def test_active_session_error_omits_abandoned_hint_for_open_skill_run(
        self, db_path, config_path, monkeypatch
    ):
        """A live skill-run keeps the existing active-session guidance unchanged."""
        monkeypatch.setattr(tusk_task_start, "_current_repo_root", lambda: "/repo")
        monkeypatch.setattr(
            tusk_task_start,
            "_unmerged_task_commits",
            lambda task_id, repo_root: ["abc1234"] if task_id else [],
            raising=False,
        )
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Live session task", status="In Progress")
            insert_criterion(conn, task_id, "do the thing")
            conn.execute(
                "INSERT INTO task_sessions (task_id, started_at) "
                "VALUES (?, datetime('now'))",
                (task_id,),
            )
            conn.execute(
                "INSERT INTO skill_runs (skill_name, task_id) VALUES ('tusk', ?)",
                (task_id,),
            )
            conn.execute(
                "INSERT INTO task_workspaces (task_id, branch, workspace_path) "
                "VALUES (?, ?, ?)",
                (task_id, f"feature/TASK-{task_id}-live", "/workspace"),
            )
            conn.commit()
        finally:
            conn.close()

        rc, result, stderr = call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 2
        assert result is None
        assert "already has an active session" in stderr
        assert "likely abandoned" not in stderr
        assert "skill-run is finished" not in stderr

    def test_active_session_error_omits_abandoned_hint_without_unmerged_commits(
        self, db_path, config_path, monkeypatch
    ):
        """A closed skill-run alone is not enough to label a session abandoned."""
        monkeypatch.setattr(tusk_task_start, "_current_repo_root", lambda: "/repo")
        monkeypatch.setattr(
            tusk_task_start,
            "_unmerged_task_commits",
            lambda task_id, repo_root: [],
        )
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            task_id = insert_task(conn, "Closed run no commits task", status="In Progress")
            insert_criterion(conn, task_id, "do the thing")
            conn.execute(
                "INSERT INTO task_sessions (task_id, started_at) "
                "VALUES (?, datetime('now'))",
                (task_id,),
            )
            conn.execute(
                "INSERT INTO skill_runs (skill_name, task_id, ended_at) "
                "VALUES ('tusk', ?, datetime('now'))",
                (task_id,),
            )
            conn.execute(
                "INSERT INTO task_workspaces (task_id, branch, workspace_path) "
                "VALUES (?, ?, ?)",
                (task_id, f"feature/TASK-{task_id}-no-commits", "/workspace"),
            )
            conn.commit()
        finally:
            conn.close()

        rc, result, stderr = call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 2
        assert result is None
        assert "already has an active session" in stderr
        assert "likely abandoned" not in stderr
        assert "skill-run is finished" not in stderr

    def test_explicit_task_id_path_unchanged(self, db_path, config_path):
        """CID 434(c): passing an explicit task_id still starts that specific task
        (regression guard — the fused path only activates when task_id is omitted)."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            # Insert a high-priority task AND a lower-priority explicit target.
            insert_task(conn, "High priority task not chosen", priority_score=100)
            target_id = insert_task(conn, "Explicit target", priority_score=20)
            insert_criterion(conn, target_id, "do the target thing")
        finally:
            conn.close()

        rc, result, _ = call_start(db_path, config_path, str(target_id), "--force")

        assert rc == 0
        assert result is not None
        assert result["task"]["id"] == target_id
        assert result["task"]["summary"] == "Explicit target"
        assert result["task"]["status"] == "In Progress"
