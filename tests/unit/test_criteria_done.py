"""Unit tests for tusk criteria done — bulk close, partial failure, already-completed.

Uses an in-memory SQLite DB — no filesystem or subprocess required.
Tests the _done_single helper and cmd_done orchestrator directly.
"""

import argparse
import importlib.util
import io
import os
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_criteria",
    os.path.join(REPO_ROOT, "bin", "tusk-criteria.py"),
)
criteria_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(criteria_mod)


def make_db(task_count=1, criteria_specs=None):
    """Return an in-memory DB with tasks, acceptance_criteria, and task_sessions tables.

    criteria_specs: list of dicts with keys criterion_type, verification_spec, is_completed.
    If None, creates 3 manual incomplete criteria on task 1.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, summary TEXT, status TEXT DEFAULT 'To Do')"
    )
    conn.execute(
        "CREATE TABLE acceptance_criteria ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  task_id INTEGER, criterion TEXT, source TEXT DEFAULT 'original',"
        "  is_completed INTEGER DEFAULT 0, is_deferred INTEGER DEFAULT 0,"
        "  deferred_reason TEXT,"
        "  criterion_type TEXT DEFAULT 'manual', verification_spec TEXT,"
        "  verification_result TEXT,"
        "  commit_hash TEXT, committed_at TEXT,"
        "  completed_at TEXT, updated_at TEXT,"
        "  cost_dollars REAL, tokens_in INTEGER, tokens_out INTEGER"
        ")"
    )
    conn.execute(
        "CREATE TABLE task_sessions ("
        "  id INTEGER PRIMARY KEY, task_id INTEGER, started_at TEXT, ended_at TEXT"
        ")"
    )
    for i in range(1, task_count + 1):
        conn.execute("INSERT INTO tasks (id, summary) VALUES (?, ?)", (i, f"Task {i}"))

    if criteria_specs is None:
        criteria_specs = [
            {"criterion_type": "manual", "verification_spec": None, "is_completed": 0},
            {"criterion_type": "manual", "verification_spec": None, "is_completed": 0},
            {"criterion_type": "manual", "verification_spec": None, "is_completed": 0},
        ]

    for spec in criteria_specs:
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, criterion_type, verification_spec, is_completed) "
            "VALUES (1, ?, ?, ?, ?)",
            (
                f"Criterion ({spec['criterion_type']})",
                spec["criterion_type"],
                spec.get("verification_spec"),
                spec.get("is_completed", 0),
            ),
        )
    conn.commit()
    return conn


# ── _done_single tests ──────────────────────────────────────────────


class TestDoneSingle:
    """Tests for the _done_single helper."""

    def test_marks_criterion_done(self):
        conn = make_db()
        out = io.StringIO()
        with redirect_stdout(out), \
             patch.object(criteria_mod, "capture_criterion_cost"):
            rc = criteria_mod._done_single(conn, 1, skip_verify=False,
                                           suppress_shared_commit=True,
                                           commit_hash=None, committed_at=None)
        assert rc == 0
        row = conn.execute("SELECT is_completed FROM acceptance_criteria WHERE id = 1").fetchone()
        assert row["is_completed"] == 1
        assert "marked done" in out.getvalue()

    def test_not_found_returns_2(self):
        conn = make_db()
        err = io.StringIO()
        with redirect_stderr(err), \
             patch.object(criteria_mod, "capture_criterion_cost"):
            rc = criteria_mod._done_single(conn, 999, skip_verify=False,
                                           suppress_shared_commit=True,
                                           commit_hash=None, committed_at=None)
        assert rc == 2
        assert "not found" in err.getvalue()

    def test_already_completed_returns_0(self):
        conn = make_db(criteria_specs=[
            {"criterion_type": "manual", "verification_spec": None, "is_completed": 1},
        ])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = criteria_mod._done_single(conn, 1, skip_verify=False,
                                           suppress_shared_commit=True,
                                           commit_hash=None, committed_at=None)
        assert rc == 0
        assert "already completed" in out.getvalue()

    def test_verification_failure_returns_1(self):
        conn = make_db(criteria_specs=[
            {"criterion_type": "test", "verification_spec": "false", "is_completed": 0},
        ])
        err = io.StringIO()
        with redirect_stderr(err), \
             patch.object(criteria_mod, "run_verification",
                          return_value={"passed": False, "output": "test failed"}):
            rc = criteria_mod._done_single(conn, 1, skip_verify=False,
                                           suppress_shared_commit=True,
                                           commit_hash=None, committed_at=None)
        assert rc == 1
        assert "FAILED" in err.getvalue()
        # Should NOT be marked done
        row = conn.execute("SELECT is_completed FROM acceptance_criteria WHERE id = 1").fetchone()
        assert row["is_completed"] == 0

    def test_skip_verify_bypasses_verification(self):
        conn = make_db(criteria_specs=[
            {"criterion_type": "test", "verification_spec": "false", "is_completed": 0},
        ])
        out = io.StringIO()
        with redirect_stdout(out), \
             patch.object(criteria_mod, "capture_criterion_cost"):
            rc = criteria_mod._done_single(conn, 1, skip_verify=True,
                                           suppress_shared_commit=True,
                                           commit_hash=None, committed_at=None)
        assert rc == 0
        assert "verification skipped" in out.getvalue()

    def test_shared_commit_warning_when_not_suppressed(self):
        conn = make_db(criteria_specs=[
            {"criterion_type": "manual", "verification_spec": None, "is_completed": 1},
            {"criterion_type": "manual", "verification_spec": None, "is_completed": 0},
        ])
        # Mark criterion 1 with a commit hash
        conn.execute(
            "UPDATE acceptance_criteria SET commit_hash = 'abc1234' WHERE id = 1"
        )
        conn.commit()
        err = io.StringIO()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
             patch.object(criteria_mod, "capture_criterion_cost"):
            rc = criteria_mod._done_single(conn, 2, skip_verify=False,
                                           suppress_shared_commit=False,
                                           commit_hash="abc1234", committed_at=None)
        assert rc == 0
        assert "Warning" in err.getvalue()

    def test_shared_commit_warning_suppressed(self):
        conn = make_db(criteria_specs=[
            {"criterion_type": "manual", "verification_spec": None, "is_completed": 1},
            {"criterion_type": "manual", "verification_spec": None, "is_completed": 0},
        ])
        conn.execute(
            "UPDATE acceptance_criteria SET commit_hash = 'abc1234' WHERE id = 1"
        )
        conn.commit()
        err = io.StringIO()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
             patch.object(criteria_mod, "capture_criterion_cost"):
            rc = criteria_mod._done_single(conn, 2, skip_verify=False,
                                           suppress_shared_commit=True,
                                           commit_hash="abc1234", committed_at=None)
        assert rc == 0
        assert "Warning" not in err.getvalue()


# ── cmd_done tests (bulk orchestration) ─────────────────────────────


class TestCmdDoneBulk:
    """Tests for cmd_done handling multiple criterion IDs.

    cmd_done calls conn.close() in a finally block. To inspect DB state after,
    we spy on _done_single calls or verify via stdout/stderr output.
    For tests that need DB-level assertions, we test _done_single directly.
    """

    def _make_args(self, ids, skip_verify=False, batch=False, allow_shared=False):
        return argparse.Namespace(
            criterion_ids=ids,
            skip_verify=skip_verify,
            batch=batch,
            allow_shared_commit=allow_shared,
        )

    def test_bulk_happy_path(self):
        """All three criteria marked done, exit 0."""
        conn = make_db()
        args = self._make_args([1, 2, 3])
        out = io.StringIO()
        with redirect_stdout(out), \
             patch.object(criteria_mod, "get_connection", return_value=conn), \
             patch.object(criteria_mod, "capture_criterion_cost"), \
             patch("subprocess.check_output", side_effect=Exception("no git")):
            rc = criteria_mod.cmd_done(args, ":memory:", {})
        assert rc == 0
        output = out.getvalue()
        assert "Criterion #1 marked done" in output
        assert "Criterion #2 marked done" in output
        assert "Criterion #3 marked done" in output

    def test_bulk_partial_failure(self):
        """Second criterion fails verification; first and third still marked done."""
        conn = make_db(criteria_specs=[
            {"criterion_type": "manual", "verification_spec": None, "is_completed": 0},
            {"criterion_type": "test", "verification_spec": "exit 1", "is_completed": 0},
            {"criterion_type": "manual", "verification_spec": None, "is_completed": 0},
        ])
        args = self._make_args([1, 2, 3])
        out = io.StringIO()
        err = io.StringIO()

        def mock_verify(ctype, spec):
            if spec == "exit 1":
                return {"passed": False, "output": "command failed"}
            return {"passed": True, "output": ""}

        with redirect_stdout(out), redirect_stderr(err), \
             patch.object(criteria_mod, "get_connection", return_value=conn), \
             patch.object(criteria_mod, "capture_criterion_cost"), \
             patch.object(criteria_mod, "run_verification", side_effect=mock_verify), \
             patch("subprocess.check_output", side_effect=Exception("no git")):
            rc = criteria_mod.cmd_done(args, ":memory:", {})

        assert rc == 1  # Non-zero because criterion 2 failed
        assert "Criterion #1 marked done" in out.getvalue()
        assert "FAILED" in err.getvalue() and "#2" in err.getvalue()
        assert "Criterion #3 marked done" in out.getvalue()

    def test_bulk_partial_failure_db_state(self):
        """Verify DB state: passed criteria done, failed one not done (via _done_single)."""
        conn = make_db(criteria_specs=[
            {"criterion_type": "manual", "verification_spec": None, "is_completed": 0},
            {"criterion_type": "test", "verification_spec": "exit 1", "is_completed": 0},
            {"criterion_type": "manual", "verification_spec": None, "is_completed": 0},
        ])

        def mock_verify(ctype, spec):
            if spec == "exit 1":
                return {"passed": False, "output": "command failed"}
            return {"passed": True, "output": ""}

        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
             patch.object(criteria_mod, "capture_criterion_cost"), \
             patch.object(criteria_mod, "run_verification", side_effect=mock_verify):
            rc1 = criteria_mod._done_single(conn, 1, False, True, None, None)
            rc2 = criteria_mod._done_single(conn, 2, False, True, None, None)
            rc3 = criteria_mod._done_single(conn, 3, False, True, None, None)

        assert rc1 == 0
        assert rc2 == 1
        assert rc3 == 0
        assert conn.execute("SELECT is_completed FROM acceptance_criteria WHERE id = 1").fetchone()["is_completed"] == 1
        assert conn.execute("SELECT is_completed FROM acceptance_criteria WHERE id = 2").fetchone()["is_completed"] == 0
        assert conn.execute("SELECT is_completed FROM acceptance_criteria WHERE id = 3").fetchone()["is_completed"] == 1

    def test_bulk_already_completed(self):
        """Already-completed criteria are silently skipped, exit 0."""
        conn = make_db(criteria_specs=[
            {"criterion_type": "manual", "verification_spec": None, "is_completed": 1},
            {"criterion_type": "manual", "verification_spec": None, "is_completed": 0},
        ])
        args = self._make_args([1, 2])
        out = io.StringIO()
        with redirect_stdout(out), \
             patch.object(criteria_mod, "get_connection", return_value=conn), \
             patch.object(criteria_mod, "capture_criterion_cost"), \
             patch("subprocess.check_output", side_effect=Exception("no git")):
            rc = criteria_mod.cmd_done(args, ":memory:", {})
        assert rc == 0
        assert "already completed" in out.getvalue()
        assert "Criterion #2 marked done" in out.getvalue()

    def test_bulk_implies_batch_for_second_plus(self):
        """In bulk mode, 2nd+ criteria suppress shared-commit warning automatically."""
        conn = make_db()
        args = self._make_args([1, 2, 3])
        calls = []
        orig_done_single = criteria_mod._done_single

        def spy_done_single(conn, cid, skip_verify, suppress, commit_hash, committed_at):
            calls.append({"cid": cid, "suppress": suppress})
            return orig_done_single(conn, cid, skip_verify, suppress, commit_hash, committed_at)

        out = io.StringIO()
        with redirect_stdout(out), \
             patch.object(criteria_mod, "get_connection", return_value=conn), \
             patch.object(criteria_mod, "_done_single", side_effect=spy_done_single), \
             patch("subprocess.check_output", side_effect=Exception("no git")):
            criteria_mod.cmd_done(args, ":memory:", {})

        # First criterion: suppress=False (no batch, no allow-shared, i=0)
        assert calls[0]["suppress"] is False
        # Second and third: suppress=True (i > 0 with len > 1)
        assert calls[1]["suppress"] is True
        assert calls[2]["suppress"] is True

    def test_single_id_backward_compatible(self):
        """A single criterion ID still works (backward compatibility)."""
        conn = make_db()
        args = self._make_args([1])
        out = io.StringIO()
        with redirect_stdout(out), \
             patch.object(criteria_mod, "get_connection", return_value=conn), \
             patch.object(criteria_mod, "capture_criterion_cost"), \
             patch("subprocess.check_output", side_effect=Exception("no git")):
            rc = criteria_mod.cmd_done(args, ":memory:", {})
        assert rc == 0
        assert "Criterion #1 marked done" in out.getvalue()

    def test_not_found_in_bulk_still_processes_others(self):
        """A not-found ID returns 2 but other criteria are still processed."""
        conn = make_db()
        args = self._make_args([999, 1, 2])
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
             patch.object(criteria_mod, "get_connection", return_value=conn), \
             patch.object(criteria_mod, "capture_criterion_cost"), \
             patch("subprocess.check_output", side_effect=Exception("no git")):
            rc = criteria_mod.cmd_done(args, ":memory:", {})
        assert rc == 2  # Worst exit code
        assert "999 not found" in err.getvalue()
        assert "Criterion #1 marked done" in out.getvalue()
        assert "Criterion #2 marked done" in out.getvalue()
