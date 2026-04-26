"""Unit tests for tusk criteria done: zero-new-commits feature branch suppression
of the 'shares commit' / 'big-bang commit' warnings (issue #562).

When a feature branch has no exclusive commits over the default branch, HEAD is
the inherited default-branch tip — not a commit produced by the current task.
Stamping criteria with that hash leaks an unrelated commit into the audit trail
and triggers spurious shared-commit warnings between unrelated criteria.

cmd_done must detect the no-new-commits case via _has_new_commits_over_default()
and nullify commit_hash/committed_at before any criterion is marked done.
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
    """Proxy that forwards everything to the wrapped connection but no-ops close().

    cmd_done closes its connection in a finally block; the test needs to inspect
    the in-memory DB afterward, so we hand cmd_done a wrapper whose close() is a
    no-op and call the real close() once the test is done.
    """

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self):
        pass


def make_db():
    """In-memory DB with two pre-completed criteria sharing the inherited HEAD hash,
    and two incomplete criteria to be marked done."""
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
    conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'Triage task')")
    # Two pre-completed criteria already stamped with the inherited HEAD hash.
    # Without the suppression, marking criteria 3+4 would print the shares-commit
    # warning twice (once for each, against criteria 1 and 2 respectively).
    conn.execute(
        "INSERT INTO acceptance_criteria "
        "(id, task_id, criterion, criterion_type, is_completed, commit_hash) "
        "VALUES (1, 1, 'C1', 'manual', 1, 'deadbee')"
    )
    conn.execute(
        "INSERT INTO acceptance_criteria "
        "(id, task_id, criterion, criterion_type, is_completed, commit_hash) "
        "VALUES (2, 1, 'C2', 'manual', 1, 'deadbee')"
    )
    conn.execute(
        "INSERT INTO acceptance_criteria "
        "(id, task_id, criterion, criterion_type, is_completed) "
        "VALUES (3, 1, 'C3', 'manual', 0)"
    )
    conn.execute(
        "INSERT INTO acceptance_criteria "
        "(id, task_id, criterion, criterion_type, is_completed) "
        "VALUES (4, 1, 'C4', 'manual', 0)"
    )
    conn.commit()
    return conn


def _make_args(ids, allow_shared=False):
    return argparse.Namespace(
        criterion_ids=ids,
        skip_verify=False,
        batch=False,
        allow_shared_commit=allow_shared,
        note=None,
    )


class TestNoNewCommitsSuppression:
    """cmd_done suppresses commit_hash stamping when branch has no new commits."""

    def _patch_git_head(self, head_hash="deadbee", head_iso="2026-04-25T12:00:00-07:00"):
        """Mock subprocess.check_output for the HEAD hash + timestamp captures."""
        def fake_check_output(cmd, **kwargs):
            if cmd[:3] == ["git", "rev-parse", "--short"]:
                return head_hash
            if cmd[:3] == ["git", "log", "-1"]:
                return head_iso
            raise Exception(f"unexpected check_output: {cmd}")
        return fake_check_output

    def test_commit_hash_nullified_when_no_new_commits(self):
        """When _has_new_commits_over_default() returns False, criteria are stored with NULL hash."""
        conn = make_db()
        proxy = _NoCloseConn(conn)
        args = _make_args([3])
        with patch.object(criteria_mod, "get_connection", return_value=proxy), \
             patch.object(criteria_mod, "capture_criterion_cost"), \
             patch.object(criteria_mod, "_has_new_commits_over_default", return_value=False), \
             patch("subprocess.check_output", side_effect=self._patch_git_head()), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = criteria_mod.cmd_done(args, ":memory:", {})
        assert rc == 0
        row = conn.execute("SELECT commit_hash, committed_at FROM acceptance_criteria WHERE id = 3").fetchone()
        assert row["commit_hash"] is None, (
            f"Expected NULL commit_hash on no-new-commits branch, got: {row['commit_hash']}"
        )
        assert row["committed_at"] is None

    def test_no_shares_commit_warning_when_no_new_commits(self):
        """No 'shares commit' warning is emitted when no exclusive commits exist."""
        conn = make_db()
        proxy = _NoCloseConn(conn)
        args = _make_args([3, 4])  # Both new criteria; without suppression, #4 would warn.
        err = io.StringIO()
        with patch.object(criteria_mod, "get_connection", return_value=proxy), \
             patch.object(criteria_mod, "capture_criterion_cost"), \
             patch.object(criteria_mod, "_has_new_commits_over_default", return_value=False), \
             patch("subprocess.check_output", side_effect=self._patch_git_head()), \
             redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = criteria_mod.cmd_done(args, ":memory:", {})
        assert rc == 0
        out = err.getvalue()
        assert "shares commit" not in out, (
            f"Expected NO 'shares commit' warning when branch has no new commits:\n{out}"
        )
        assert "big-bang" not in out, (
            f"Expected NO 'big-bang commit' warning when branch has no new commits:\n{out}"
        )

    def test_commit_hash_preserved_when_branch_has_new_commits(self):
        """When _has_new_commits_over_default() returns True, HEAD hash is stamped as before."""
        conn = make_db()
        proxy = _NoCloseConn(conn)
        args = _make_args([3])
        with patch.object(criteria_mod, "get_connection", return_value=proxy), \
             patch.object(criteria_mod, "capture_criterion_cost"), \
             patch.object(criteria_mod, "_has_new_commits_over_default", return_value=True), \
             patch.object(criteria_mod, "_head_task_id", return_value=1), \
             patch("subprocess.check_output", side_effect=self._patch_git_head("abc1234")), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = criteria_mod.cmd_done(args, ":memory:", {})
        assert rc == 0
        row = conn.execute("SELECT commit_hash FROM acceptance_criteria WHERE id = 3").fetchone()
        assert row["commit_hash"] == "abc1234", (
            f"Expected commit_hash to be stamped on a branch with new commits, got: {row['commit_hash']}"
        )

    def test_fails_open_when_detection_errors(self):
        """If _has_new_commits_over_default() returns True (its fail-open default),
        existing behavior is preserved — HEAD hash gets stamped as before."""
        conn = make_db()
        proxy = _NoCloseConn(conn)
        args = _make_args([3])
        # Default fail-open returns True — same as branch-with-new-commits path.
        with patch.object(criteria_mod, "get_connection", return_value=proxy), \
             patch.object(criteria_mod, "capture_criterion_cost"), \
             patch.object(criteria_mod, "_has_new_commits_over_default", return_value=True), \
             patch.object(criteria_mod, "_head_task_id", return_value=1), \
             patch("subprocess.check_output", side_effect=self._patch_git_head("feedbac")), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            criteria_mod.cmd_done(args, ":memory:", {})
        row = conn.execute("SELECT commit_hash FROM acceptance_criteria WHERE id = 3").fetchone()
        assert row["commit_hash"] == "feedbac"


class TestHasNewCommitsHelper:
    """Direct tests for the _has_new_commits_over_default helper."""

    def test_returns_true_when_count_nonzero(self):
        """Count > 0 → branch has exclusive commits → True."""
        from subprocess import CompletedProcess

        def fake_run(cmd, **kwargs):
            if "git-default-branch" in cmd:
                return CompletedProcess(cmd, 0, stdout="main\n", stderr="")
            if cmd[:3] == ["git", "rev-list", "--count"]:
                return CompletedProcess(cmd, 0, stdout="3\n", stderr="")
            return CompletedProcess(cmd, 1, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            assert criteria_mod._has_new_commits_over_default() is True

    def test_returns_false_when_count_zero(self):
        """Count == 0 → no exclusive commits → False."""
        from subprocess import CompletedProcess

        def fake_run(cmd, **kwargs):
            if "git-default-branch" in cmd:
                return CompletedProcess(cmd, 0, stdout="main\n", stderr="")
            if cmd[:3] == ["git", "rev-list", "--count"]:
                return CompletedProcess(cmd, 0, stdout="0\n", stderr="")
            return CompletedProcess(cmd, 1, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            assert criteria_mod._has_new_commits_over_default() is False

    def test_fails_open_when_default_branch_unresolved(self):
        """If `tusk git-default-branch` returns empty/error, fail open (return True)."""
        from subprocess import CompletedProcess

        def fake_run(cmd, **kwargs):
            if "git-default-branch" in cmd:
                return CompletedProcess(cmd, 0, stdout="", stderr="")
            return CompletedProcess(cmd, 1, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            assert criteria_mod._has_new_commits_over_default() is True

    def test_fails_open_when_rev_list_errors(self):
        """If `git rev-list --count` exits non-zero, fail open (return True)."""
        from subprocess import CompletedProcess

        def fake_run(cmd, **kwargs):
            if "git-default-branch" in cmd:
                return CompletedProcess(cmd, 0, stdout="main\n", stderr="")
            if cmd[:3] == ["git", "rev-list", "--count"]:
                return CompletedProcess(cmd, 128, stdout="", stderr="bad ref")
            return CompletedProcess(cmd, 1, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            assert criteria_mod._has_new_commits_over_default() is True

    def test_fails_open_on_exception(self):
        """Any exception → fail open (return True)."""
        with patch("subprocess.run", side_effect=OSError("git not installed")):
            assert criteria_mod._has_new_commits_over_default() is True
