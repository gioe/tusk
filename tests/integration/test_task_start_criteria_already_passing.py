"""Integration tests for task-start's convergent-completion signal (issue #1051).

A sibling task may ship this task's deliverables before pickup, leaving the
disk in a state where code/file-type acceptance criteria already pass before
any work begins. task-start runs those verification specs read-only at start,
emits criteria_already_passing in its JSON output, and forces
deliverable_check_needed=true when the count is positive so the /tusk skill
routes through check-deliverables instead of re-implementing shipped work.
"""

import importlib.util
import io
import json
import os
import sqlite3
import sys
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

def insert_task(conn: sqlite3.Connection, summary: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO tasks (summary, status, priority, complexity, task_type,
                           priority_score, description)
        VALUES (?, 'To Do', 'Medium', 'S', 'feature', 60, '')
        """,
        (summary,),
    )
    conn.commit()
    return cur.lastrowid


def insert_criterion(
    conn: sqlite3.Connection,
    task_id: int,
    text: str,
    *,
    criterion_type: str = "manual",
    verification_spec: str | None = None,
    is_completed: int = 0,
    is_deferred: int = 0,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO acceptance_criteria
            (task_id, criterion, source, is_completed, is_deferred,
             criterion_type, verification_spec)
        VALUES (?, ?, 'original', ?, ?, ?, ?)
        """,
        (task_id, text, is_completed, is_deferred, criterion_type, verification_spec),
    )
    conn.commit()
    return cur.lastrowid


def call_start(db_path, config_path, *extra_args) -> tuple[int, dict | None, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_task_start.main([str(db_path), str(config_path), *extra_args])
    out = out_buf.getvalue().strip()
    result = json.loads(out) if out else None
    return rc, result, err_buf.getvalue()


@pytest.fixture
def conn(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCriteriaAlreadyPassing:
    def test_passing_code_spec_counted_and_forces_deliverable_check(
        self, conn, db_path, config_path
    ):
        task_id = insert_task(conn, "convergent completion repro")
        insert_criterion(
            conn, task_id, "always passes",
            criterion_type="code", verification_spec="true",
        )

        rc, result, stderr = call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 0
        assert result["criteria_already_passing"] == 1
        assert result["deliverable_check_needed"] is True
        assert "possible convergent completion" in stderr

    def test_failing_code_spec_not_counted(self, conn, db_path, config_path):
        task_id = insert_task(conn, "spec fails on current disk")
        insert_criterion(
            conn, task_id, "not yet satisfied",
            criterion_type="code", verification_spec="false",
        )

        rc, result, stderr = call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 0
        assert result["criteria_already_passing"] == 0
        assert result["deliverable_check_needed"] is False
        assert "possible convergent completion" not in stderr

    def test_manual_criteria_not_executed(self, conn, db_path, config_path, tmp_path):
        # A manual criterion's spec must never run: a passing spec on a manual
        # row is noise, not deliverable evidence (same reasoning as
        # check-deliverables' manual_pending recommendation, issue #806).
        marker = tmp_path / "manual-spec-ran"
        task_id = insert_task(conn, "manual criteria task")
        insert_criterion(
            conn, task_id, "operator does external work",
            criterion_type="manual",
            verification_spec=f"touch {marker}",
        )

        rc, result, _ = call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 0
        assert result["criteria_already_passing"] == 0
        assert result["deliverable_check_needed"] is False
        assert not marker.exists()

    def test_test_type_criteria_not_executed(self, conn, db_path, config_path, tmp_path):
        # test-type specs (full suite runs) are excluded from the start-time
        # scan for latency; only code/file types run.
        marker = tmp_path / "test-spec-ran"
        task_id = insert_task(conn, "test criterion task")
        insert_criterion(
            conn, task_id, "suite passes",
            criterion_type="test",
            verification_spec=f"touch {marker}",
        )

        rc, result, _ = call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 0
        assert result["criteria_already_passing"] == 0
        assert not marker.exists()

    def test_no_criteria_task_emits_zero_unchanged_behavior(
        self, conn, db_path, config_path
    ):
        task_id = insert_task(conn, "zero criteria task")

        rc, result, _ = call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 0
        assert result["criteria_already_passing"] == 0
        assert result["deliverable_check_needed"] is False

    def test_completed_and_deferred_criteria_excluded(
        self, conn, db_path, config_path
    ):
        task_id = insert_task(conn, "completed and deferred excluded")
        insert_criterion(
            conn, task_id, "already done",
            criterion_type="code", verification_spec="true", is_completed=1,
        )
        insert_criterion(
            conn, task_id, "deferred",
            criterion_type="code", verification_spec="true", is_deferred=1,
        )

        rc, result, _ = call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 0
        assert result["criteria_already_passing"] == 0
        # deliverable_check_needed stays true via the completed-criteria proxy.
        assert result["deliverable_check_needed"] is True

    def test_file_spec_counted_when_file_exists(
        self, conn, db_path, config_path, tmp_path
    ):
        deliverable = tmp_path / "deliverable.txt"
        deliverable.write_text("shipped\n", encoding="utf-8")
        task_id = insert_task(conn, "file criterion task")
        insert_criterion(
            conn, task_id, "deliverable exists",
            criterion_type="file", verification_spec=str(deliverable),
        )

        rc, result, _ = call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 0
        assert result["criteria_already_passing"] == 1
        assert result["deliverable_check_needed"] is True

    def test_malformed_spec_counts_as_not_passing(self, conn, db_path, config_path):
        task_id = insert_task(conn, "malformed spec task")
        insert_criterion(
            conn, task_id, "broken spec",
            criterion_type="code",
            verification_spec="definitely-not-a-command-xyz-624",
        )

        rc, result, _ = call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 0
        assert result["criteria_already_passing"] == 0
        assert result["deliverable_check_needed"] is False

    def test_runner_exception_degrades_to_zero(
        self, conn, db_path, config_path, monkeypatch
    ):
        # Best-effort guarantee: an exploding verification runner must never
        # crash task-start — the scan degrades to 0.
        criteria_mod = sys.modules.get("tusk_criteria")
        if criteria_mod is None:
            criteria_mod = tusk_task_start.tusk_loader.load("tusk-criteria")

        def boom(*args, **kwargs):
            raise RuntimeError("runner exploded")

        monkeypatch.setattr(criteria_mod, "run_verification", boom)

        task_id = insert_task(conn, "runner exception task")
        insert_criterion(
            conn, task_id, "would pass",
            criterion_type="code", verification_spec="true",
        )

        rc, result, _ = call_start(db_path, config_path, str(task_id), "--force")

        assert rc == 0
        assert result["criteria_already_passing"] == 0
        assert result["deliverable_check_needed"] is False
