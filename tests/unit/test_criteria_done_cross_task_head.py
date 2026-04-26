"""Unit tests for tusk criteria done: cross-task HEAD suppression (issue #573).

When sequential tasks share a branch (TASK-A's commit lands, then TASK-B's
manual criteria are closed via --skip-verify on the same branch), the existing
_has_new_commits_over_default() guard does NOT fire — the branch DOES have
exclusive commits over default — so without the cross-task guard, TASK-A's
HEAD hash is stamped onto TASK-B's criteria, polluting the audit trail.

cmd_done must call _head_task_id() to extract HEAD's [TASK-<n>] reference and
pass it to _done_single, which nullifies commit_hash/committed_at when the
parsed task ID does not match the criterion's task_id.
"""

import argparse
import importlib.util
import io
import os
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_criteria",
    os.path.join(REPO_ROOT, "bin", "tusk-criteria.py"),
)
criteria_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(criteria_mod)


class _NoCloseConn:
    """Forwarding proxy whose close() is a no-op so the test can inspect the DB
    after cmd_done's finally-block close() runs."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self):
        pass


def make_db():
    """In-memory DB with two tasks (A=100, B=200) and one incomplete manual criterion on each."""
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
        "  completed_at TEXT, updated_at TEXT, created_at TEXT,"
        "  cost_dollars REAL, tokens_in INTEGER, tokens_out INTEGER,"
        "  skip_note TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE task_sessions ("
        "  id INTEGER PRIMARY KEY, task_id INTEGER, started_at TEXT, ended_at TEXT"
        ")"
    )
    conn.execute("INSERT INTO tasks (id, summary) VALUES (100, 'Task A')")
    conn.execute("INSERT INTO tasks (id, summary) VALUES (200, 'Task B')")
    conn.execute(
        "INSERT INTO acceptance_criteria (id, task_id, criterion, criterion_type, is_completed) "
        "VALUES (1, 100, 'CA', 'manual', 0)"
    )
    conn.execute(
        "INSERT INTO acceptance_criteria (id, task_id, criterion, criterion_type, is_completed) "
        "VALUES (2, 200, 'CB', 'manual', 0)"
    )
    conn.commit()
    return conn


def _make_args(ids):
    return argparse.Namespace(
        criterion_ids=ids,
        skip_verify=True,
        batch=False,
        allow_shared_commit=False,
        note=None,
    )


def _patch_git_head(head_hash):
    """subprocess.check_output stub that returns the HEAD hash and timestamp."""
    def fake(cmd, **kwargs):
        if cmd[:3] == ["git", "rev-parse", "--short"]:
            return head_hash
        if cmd[:3] == ["git", "log", "-1"]:
            return "2026-04-26T00:00:00-07:00"
        raise Exception(f"unexpected check_output: {cmd}")
    return fake


class TestCrossTaskHeadSuppression:
    """cmd_done suppresses commit_hash when HEAD's [TASK-N] points to a different task."""

    def test_cross_task_head_does_not_stamp(self):
        """Issue #573 repro: HEAD is [TASK-100] commit, criterion belongs to TASK-200.
        commit_hash must NOT be stamped onto criterion 2 (the cross-task case)."""
        conn = make_db()
        proxy = _NoCloseConn(conn)
        # Closing TASK-200's criterion 2 — its task_id is 200.
        args = _make_args([2])
        with patch.object(criteria_mod, "get_connection", return_value=proxy), \
             patch.object(criteria_mod, "capture_criterion_cost"), \
             patch.object(criteria_mod, "_has_new_commits_over_default", return_value=True), \
             patch.object(criteria_mod, "_head_task_id", return_value=100), \
             patch("subprocess.check_output", side_effect=_patch_git_head("9233c04c")), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = criteria_mod.cmd_done(args, ":memory:", {})
        assert rc == 0
        row = conn.execute(
            "SELECT commit_hash, committed_at, is_completed FROM acceptance_criteria WHERE id = 2"
        ).fetchone()
        assert row["is_completed"] == 1, "Criterion should still be marked done"
        assert row["commit_hash"] is None, (
            f"Expected NULL commit_hash on cross-task HEAD, got: {row['commit_hash']}"
        )
        assert row["committed_at"] is None

    def test_head_with_no_task_prefix_does_not_stamp(self):
        """HEAD has no [TASK-N] prefix (e.g., default-branch tip predating tusk).
        _head_task_id() returns None — no match for any criterion → no stamp."""
        conn = make_db()
        proxy = _NoCloseConn(conn)
        args = _make_args([1])  # criterion 1 belongs to TASK-100
        with patch.object(criteria_mod, "get_connection", return_value=proxy), \
             patch.object(criteria_mod, "capture_criterion_cost"), \
             patch.object(criteria_mod, "_has_new_commits_over_default", return_value=True), \
             patch.object(criteria_mod, "_head_task_id", return_value=None), \
             patch("subprocess.check_output", side_effect=_patch_git_head("abc1234")), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = criteria_mod.cmd_done(args, ":memory:", {})
        assert rc == 0
        row = conn.execute(
            "SELECT commit_hash FROM acceptance_criteria WHERE id = 1"
        ).fetchone()
        assert row["commit_hash"] is None


class TestSingleTaskHeadStamps:
    """Regression guard: when HEAD's [TASK-N] matches the criterion's task, stamping proceeds."""

    def test_matching_head_task_stamps(self):
        """HEAD is [TASK-100] commit, criterion belongs to TASK-100 — stamp commit_hash as before."""
        conn = make_db()
        proxy = _NoCloseConn(conn)
        args = _make_args([1])  # criterion 1 belongs to TASK-100
        with patch.object(criteria_mod, "get_connection", return_value=proxy), \
             patch.object(criteria_mod, "capture_criterion_cost"), \
             patch.object(criteria_mod, "_has_new_commits_over_default", return_value=True), \
             patch.object(criteria_mod, "_head_task_id", return_value=100), \
             patch("subprocess.check_output", side_effect=_patch_git_head("abc1234")), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = criteria_mod.cmd_done(args, ":memory:", {})
        assert rc == 0
        row = conn.execute(
            "SELECT commit_hash, committed_at FROM acceptance_criteria WHERE id = 1"
        ).fetchone()
        assert row["commit_hash"] == "abc1234"
        assert row["committed_at"] == "2026-04-26T00:00:00-07:00"


class TestHeadTaskIdHelper:
    """Direct tests for the _head_task_id helper."""

    def test_parses_task_id_from_message(self):
        with patch(
            "subprocess.check_output",
            return_value="[TASK-188] Bump VERSION to 739\n\nBody line\n",
        ):
            assert criteria_mod._head_task_id() == 188

    def test_returns_none_when_no_task_prefix(self):
        with patch("subprocess.check_output", return_value="Initial commit\n"):
            assert criteria_mod._head_task_id() is None

    def test_returns_none_on_subprocess_error(self):
        with patch("subprocess.check_output", side_effect=OSError("git not installed")):
            assert criteria_mod._head_task_id() is None

    def test_parses_first_task_prefix_in_message(self):
        """The regex anchors to start-of-message: a position-zero [TASK-N] is parsed
        even when later body text references another [TASK-M], and a body-only
        reference (no position-zero prefix) returns None."""
        with patch(
            "subprocess.check_output",
            return_value="[TASK-50] Refer to [TASK-99] context\n",
        ):
            assert criteria_mod._head_task_id() == 50

        with patch(
            "subprocess.check_output",
            return_value="Initial commit (relates to [TASK-99])\n",
        ):
            assert criteria_mod._head_task_id() is None
