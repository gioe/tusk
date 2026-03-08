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
