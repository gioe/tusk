"""Regression tests for tusk-merge WAL checkpoint and task/session recovery (issue #327).

When the SQLite WAL is reverted mid-sequence (e.g. due to busy readers blocking the
checkpoint), tusk merge should:
  1. Use PRAGMA wal_checkpoint(TRUNCATE) to zero the WAL after flushing.
  2. Retry the checkpoint up to max_retries times when busy readers block it.
  3. Recover gracefully when the task row is missing post-WAL-revert.
  4. Recover gracefully when the session row is missing post-WAL-revert (pre-existing).
"""

import importlib.util
import io
import os
import sqlite3
from contextlib import redirect_stderr

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


tusk_merge = _load("tusk-merge")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_task(conn: sqlite3.Connection, *, status: str = "In Progress") -> int:
    cur = conn.execute(
        "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score)"
        " VALUES ('test task', ?, 'feature', 'Medium', 'S', 50)",
        (status,),
    )
    conn.commit()
    return cur.lastrowid


def _insert_session(conn: sqlite3.Connection, task_id: int, *, closed: bool = False) -> int:
    if closed:
        cur = conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, ended_at)"
            " VALUES (?, datetime('now', '-1 hour'), datetime('now'))",
            (task_id,),
        )
    else:
        cur = conn.execute(
            "INSERT INTO task_sessions (task_id, started_at) VALUES (?, datetime('now'))",
            (task_id,),
        )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# _checkpoint_wal tests
# ---------------------------------------------------------------------------

class TestCheckpointWal:

    def test_uses_truncate_mode(self, db_path, config_path):
        """_checkpoint_wal issues PRAGMA wal_checkpoint(TRUNCATE), not FULL."""
        issued_pragmas = []
        original_get_connection = tusk_merge.get_connection

        class _TrackingConn:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *args, **kwargs):
                issued_pragmas.append(sql)
                return self._inner.execute(sql, *args, **kwargs)

            def close(self):
                self._inner.close()

        def _patched_get_connection(path):
            return _TrackingConn(original_get_connection(path))

        tusk_merge.get_connection = _patched_get_connection
        try:
            buf = io.StringIO()
            with redirect_stderr(buf):
                tusk_merge._checkpoint_wal(str(db_path))
        finally:
            tusk_merge.get_connection = original_get_connection

        assert any("TRUNCATE" in p for p in issued_pragmas), (
            f"Expected PRAGMA wal_checkpoint(TRUNCATE) but got: {issued_pragmas}"
        )
        assert not any("FULL" in p for p in issued_pragmas), (
            "Should not use FULL mode"
        )

    def test_succeeds_on_clean_db(self, db_path, config_path):
        """_checkpoint_wal completes without error on a freshly initialised DB."""
        buf = io.StringIO()
        with redirect_stderr(buf):
            # Should not raise; should print "Checkpointing WAL..."
            tusk_merge._checkpoint_wal(str(db_path))
        assert "Checkpointing WAL" in buf.getvalue()

    def test_prints_warning_when_blocked(self, db_path, config_path, monkeypatch):
        """_checkpoint_wal warns when checkpoint is blocked after all retries."""
        # Simulate checkpoint always returning busy=1
        original_get_connection = tusk_merge.get_connection

        class _BusyConn:
            def execute(self, sql, *a, **kw):
                class _Row:
                    def fetchone(self_inner):
                        return (1, 10, 5)  # busy=1, log=10, checkpointed=5
                return _Row()

            def close(self):
                pass

        monkeypatch.setattr(tusk_merge, "get_connection", lambda p: _BusyConn())
        monkeypatch.setattr(tusk_merge.time, "sleep", lambda s: None)

        buf = io.StringIO()
        with redirect_stderr(buf):
            tusk_merge._checkpoint_wal(str(db_path), max_retries=2)

        output = buf.getvalue()
        assert "partially blocked" in output
        assert "2 attempts" in output

    def test_partial_checkpoint_not_silent(self, db_path, config_path, monkeypatch):
        """_checkpoint_wal does not return silently when busy=0 but log > checkpointed.

        A partial checkpoint (busy=0, log=10, checkpointed=5) means SQLite finished
        without busy readers but did not flush all pages — the function must retry
        and ultimately warn rather than treat it as success.
        """

        class _PartialConn:
            def execute(self, sql, *a, **kw):
                class _Row:
                    def fetchone(self_inner):
                        return (0, 10, 5)  # busy=0, but log != checkpointed
                return _Row()

            def close(self):
                pass

        monkeypatch.setattr(tusk_merge, "get_connection", lambda p: _PartialConn())
        monkeypatch.setattr(tusk_merge.time, "sleep", lambda s: None)

        buf = io.StringIO()
        with redirect_stderr(buf):
            tusk_merge._checkpoint_wal(str(db_path), max_retries=2)

        output = buf.getvalue()
        assert "partially blocked" in output, (
            "Expected a warning for partial checkpoint (busy=0, log=10, checkpointed=5) "
            f"but got: {output!r}"
        )
        assert "2 attempts" in output


# ---------------------------------------------------------------------------
# _recover_missing_task tests
# ---------------------------------------------------------------------------

class TestRecoverMissingTask:

    def test_inserts_done_record(self, db_path, config_path):
        """_recover_missing_task inserts a Done row for the given task_id."""
        task_id = 9999  # deliberately not in DB
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = tusk_merge._recover_missing_task(str(db_path), task_id)

        assert result is True
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        finally:
            conn.close()

        assert row is not None, f"Task {task_id} was not inserted"
        assert row["status"] == "Done"
        assert row["closed_reason"] == "completed"
        assert "Recovered" in row["summary"]

    def test_returns_false_on_duplicate(self, db_path, config_path):
        """_recover_missing_task returns False when task_id already exists."""
        conn = sqlite3.connect(str(db_path))
        try:
            task_id = _insert_task(conn)
        finally:
            conn.close()

        buf = io.StringIO()
        with redirect_stderr(buf):
            result = tusk_merge._recover_missing_task(str(db_path), task_id)

        assert result is False
        assert "Could not re-insert" in buf.getvalue()

    def test_logs_warning_and_recovery_message(self, db_path, config_path):
        """_recover_missing_task logs both a warning and a success message."""
        task_id = 8888
        buf = io.StringIO()
        with redirect_stderr(buf):
            tusk_merge._recover_missing_task(str(db_path), task_id)

        output = buf.getvalue()
        assert "WAL revert" in output
        assert "Recovered" in output


# ---------------------------------------------------------------------------
# main() recovery hint tests
# ---------------------------------------------------------------------------


class TestMergeRecoveryHint:
    """Tests that main() prints the recovery hint when task-done returns 'not found'
    and _recover_missing_task successfully re-inserts the placeholder row."""

    def test_hint_printed_when_recovery_succeeds(self, db_path, config_path, monkeypatch):
        """main() emits 'Hint:' and 'tusk task-update' to stderr when task-done
        reports the task as missing and recovery re-inserts it successfully."""
        import subprocess

        task_id = 7777

        # Insert an open session for the task without a task row.
        # SQLite does not enforce FK constraints by default, so this is valid.
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                "INSERT INTO task_sessions (task_id, started_at) VALUES (?, datetime('now'))",
                (task_id,),
            )
            conn.commit()
            session_id = cur.lastrowid
        finally:
            conn.close()

        # Suppress _checkpoint_wal so it doesn't touch the live WAL during tests.
        monkeypatch.setattr(tusk_merge, "_checkpoint_wal", lambda *a, **kw: None)

        # Return a fake branch name so find_task_branch doesn't shell out.
        monkeypatch.setattr(
            tusk_merge, "find_task_branch", lambda tid: (f"feature/TASK-{tid}-test", None)
        )

        # Return "main" without touching git.
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")

        # Capture which commands are dispatched.
        dispatched = []

        def _mock_run(args, check=True):
            dispatched.append(args)
            subcmd = args[1] if len(args) > 1 else ""
            # Git dirty-tree check — report clean working tree.
            if args[0] == "git" and subcmd == "diff" and "--name-only" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            # session-close — succeed.
            if subcmd == "session-close":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            # Git operations (checkout, pull, merge, push, branch -d/--delete) — succeed.
            if args[0] == "git" and subcmd in ("checkout", "pull", "merge", "push"):
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[0] == "git" and subcmd == "branch" and (
                "-d" in args or "--delete" in args
            ):
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            # task-done — simulate missing task row (WAL revert scenario).
            if subcmd == "task-done":
                return subprocess.CompletedProcess(
                    args, 2, stdout="", stderr=f"Error: task {task_id} not found"
                )
            # Fallback — succeed silently for any unexpected call.
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_merge, "run", _mock_run)

        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        output = buf.getvalue()
        assert rc == 0, f"Expected return code 0 but got {rc}. stderr:\n{output}"
        assert "Hint:" in output, f"Expected 'Hint:' in stderr but got:\n{output}"
        assert "tusk task-update" in output, (
            f"Expected 'tusk task-update' in stderr but got:\n{output}"
        )


# ---------------------------------------------------------------------------
# _detect_id_gaps tests
# ---------------------------------------------------------------------------


class TestDetectIdGaps:

    def test_no_gaps_when_contiguous(self, db_path, config_path):
        """_detect_id_gaps returns [] when all IDs below task_id are present."""
        conn = sqlite3.connect(str(db_path))
        try:
            id1 = _insert_task(conn)
            id2 = _insert_task(conn)
        finally:
            conn.close()

        # task being merged is id2; id1 exists immediately below — no gap
        result = tusk_merge._detect_id_gaps(str(db_path), id2)
        assert result == [], f"Expected no gaps but got: {result}"

    def test_detects_missing_ids_in_range(self, db_path, config_path):
        """_detect_id_gaps returns missing IDs between max existing and task_id."""
        conn = sqlite3.connect(str(db_path))
        try:
            id1 = _insert_task(conn)
        finally:
            conn.close()

        # Simulate gap: task_id is id1+3 but id1+1 and id1+2 were never committed
        task_id = id1 + 3
        result = tusk_merge._detect_id_gaps(str(db_path), task_id)
        assert result == [id1 + 1, id1 + 2], (
            f"Expected gap IDs {[id1 + 1, id1 + 2]} but got: {result}"
        )

    def test_returns_empty_when_no_tasks_below(self, db_path, config_path):
        """_detect_id_gaps returns [] when task_id is the first task ever."""
        # Use a task_id of 1; no tasks exist below it
        result = tusk_merge._detect_id_gaps(str(db_path), 1)
        assert result == []

    def test_returns_empty_for_task_id_1_with_higher_ids_present(self, db_path, config_path):
        """_detect_id_gaps returns [] for task_id=1 even when higher-ID tasks exist.

        MAX(id WHERE id < 1) is NULL regardless of what tasks exist above 1,
        so the NULL guard on row[0] must trigger and return [] correctly.
        """
        conn = sqlite3.connect(str(db_path))
        try:
            _insert_task(conn)  # inserts a task with id > 1
        finally:
            conn.close()

        result = tusk_merge._detect_id_gaps(str(db_path), 1)
        assert result == [], f"Expected [] for task_id=1 with higher IDs present, got: {result}"

    def test_returns_empty_when_immediately_adjacent(self, db_path, config_path):
        """_detect_id_gaps returns [] when max_below == task_id - 1 (no gap)."""
        conn = sqlite3.connect(str(db_path))
        try:
            id1 = _insert_task(conn)
        finally:
            conn.close()

        result = tusk_merge._detect_id_gaps(str(db_path), id1 + 1)
        assert result == []


class TestMergeWalGapWarning:
    """Tests that main() warns about gap task IDs in the WAL recovery path."""

    def _make_mock_run(self, task_id):
        import subprocess

        def _mock_run(args, check=True):
            subcmd = args[1] if len(args) > 1 else ""
            if args[0] == "git" and subcmd == "diff" and "--name-only" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if subcmd == "session-close":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[0] == "git" and subcmd in ("checkout", "pull", "merge", "push"):
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[0] == "git" and subcmd == "branch" and (
                "-d" in args or "--delete" in args
            ):
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if subcmd == "task-done":
                return subprocess.CompletedProcess(
                    args, 2, stdout="", stderr=f"Error: task {task_id} not found"
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        return _mock_run

    def test_warns_about_gap_ids_when_present(self, db_path, config_path, monkeypatch):
        """main() prints a gap warning listing missing IDs when gaps exist."""
        conn = sqlite3.connect(str(db_path))
        try:
            base_id = _insert_task(conn)
            # gap IDs: base_id+1 and base_id+2 are never inserted
            task_id = base_id + 3
            cur = conn.execute(
                "INSERT INTO task_sessions (task_id, started_at) VALUES (?, datetime('now'))",
                (task_id,),
            )
            conn.commit()
            session_id = cur.lastrowid
        finally:
            conn.close()

        monkeypatch.setattr(tusk_merge, "_checkpoint_wal", lambda *a, **kw: None)
        monkeypatch.setattr(
            tusk_merge, "find_task_branch", lambda tid: (f"feature/TASK-{tid}-test", None)
        )
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "run", self._make_mock_run(task_id))

        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        output = buf.getvalue()
        assert rc == 0, f"Expected rc=0 but got {rc}. stderr:\n{output}"
        assert "lost in the WAL revert and cannot be recovered" in output, f"Expected gap warning but got:\n{output}"
        assert str(base_id + 1) in output, f"Expected gap ID {base_id + 1} in output"
        assert str(base_id + 2) in output, f"Expected gap ID {base_id + 2} in output"

    def test_no_gap_warning_when_contiguous(self, db_path, config_path, monkeypatch):
        """main() does not print a gap warning when there are no gaps."""
        conn = sqlite3.connect(str(db_path))
        try:
            base_id = _insert_task(conn)
            task_id = base_id + 1  # immediately adjacent — no gap
            cur = conn.execute(
                "INSERT INTO task_sessions (task_id, started_at) VALUES (?, datetime('now'))",
                (task_id,),
            )
            conn.commit()
            session_id = cur.lastrowid
        finally:
            conn.close()

        monkeypatch.setattr(tusk_merge, "_checkpoint_wal", lambda *a, **kw: None)
        monkeypatch.setattr(
            tusk_merge, "find_task_branch", lambda tid: (f"feature/TASK-{tid}-test", None)
        )
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "run", self._make_mock_run(task_id))

        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        output = buf.getvalue()
        assert rc == 0
        assert "lost in the WAL revert and cannot be recovered" not in output, (
            f"Expected no gap warning for contiguous IDs but got:\n{output}"
        )


    def test_gap_ids_in_json_output(self, db_path, config_path, monkeypatch):
        """main() includes gap_task_ids in the JSON output during WAL recovery."""
        import json as _json

        conn = sqlite3.connect(str(db_path))
        try:
            base_id = _insert_task(conn)
            task_id = base_id + 2  # base_id+1 is the gap
            cur = conn.execute(
                "INSERT INTO task_sessions (task_id, started_at) VALUES (?, datetime('now'))",
                (task_id,),
            )
            conn.commit()
            session_id = cur.lastrowid
        finally:
            conn.close()

        monkeypatch.setattr(tusk_merge, "_checkpoint_wal", lambda *a, **kw: None)
        monkeypatch.setattr(
            tusk_merge, "find_task_branch", lambda tid: (f"feature/TASK-{tid}-test", None)
        )
        monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
        monkeypatch.setattr(tusk_merge, "run", self._make_mock_run(task_id))

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        import contextlib

        with contextlib.redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = tusk_merge.main(
                [str(db_path), str(config_path), str(task_id), "--session", str(session_id)]
            )

        assert rc == 0
        data = _json.loads(stdout_buf.getvalue())
        assert "gap_task_ids" in data, f"Expected gap_task_ids in JSON but got: {data}"
        assert data["gap_task_ids"] == [base_id + 1], (
            f"Expected gap_task_ids=[{base_id + 1}] but got: {data['gap_task_ids']}"
        )
