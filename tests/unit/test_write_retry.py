"""Unit tests for the transient-SQLITE_BUSY retry layer in tusk-db-lib.py.

Issue #1143: under parallel worktree sessions sharing one tasks.db, a tusk write
command could crash with ``OperationalError: database is locked`` instead of
waiting/retrying. ``PRAGMA busy_timeout`` (issue #946) does NOT cover the
lock-upgrade case — a connection holding a SHARED read lock that promotes to a
writer while another holds RESERVED gets SQLITE_BUSY immediately, with no
busy-handler wait. The fix is ``isolation_level="IMMEDIATE"`` (acquire the write
lock up front, where busy_timeout IS honored) plus a bounded whole-operation
retry (``retry_on_locked`` / ``run_write``) that drops the SHARED lock between
attempts.
"""

import importlib.util
import os
import sqlite3
import threading
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_db_lib",
    os.path.join(REPO_ROOT, "bin", "tusk-db-lib.py"),
)
db_lib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(db_lib)


# ── _is_locked_error classification ───────────────────────────────────


class TestIsLockedError:
    def test_database_is_locked_is_retryable(self):
        assert db_lib._is_locked_error(sqlite3.OperationalError("database is locked"))

    def test_database_is_busy_is_retryable(self):
        assert db_lib._is_locked_error(sqlite3.OperationalError("database is busy"))

    def test_case_insensitive(self):
        assert db_lib._is_locked_error(sqlite3.OperationalError("Database Is Locked"))

    def test_other_operational_error_not_retryable(self):
        assert not db_lib._is_locked_error(sqlite3.OperationalError("no such table: t"))

    def test_non_operational_error_not_retryable(self):
        assert not db_lib._is_locked_error(ValueError("database is locked"))


# ── retry_on_locked ───────────────────────────────────────────────────


class TestRetryOnLocked:
    def test_returns_value_on_first_success(self, monkeypatch):
        monkeypatch.setattr(db_lib.time, "sleep", lambda *_: None)
        assert db_lib.retry_on_locked(lambda: 42) == 42

    def test_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(db_lib.time, "sleep", lambda *_: None)
        calls = {"n": 0}

        def op():
            calls["n"] += 1
            if calls["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        assert db_lib.retry_on_locked(op, retries=5) == "ok"
        assert calls["n"] == 3

    def test_non_lock_error_is_not_retried(self, monkeypatch):
        monkeypatch.setattr(db_lib.time, "sleep", lambda *_: None)
        calls = {"n": 0}

        def op():
            calls["n"] += 1
            raise sqlite3.OperationalError("no such table: tasks")

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            db_lib.retry_on_locked(op, retries=5)
        assert calls["n"] == 1  # not retried

    def test_exhausting_budget_reraises_with_diagnostic(self, monkeypatch, capsys):
        monkeypatch.setattr(db_lib.time, "sleep", lambda *_: None)
        calls = {"n": 0}

        def op():
            calls["n"] += 1
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            db_lib.retry_on_locked(op, retries=2, label="skill-run finish")
        # retries=2 means 1 initial + 2 retries = 3 attempts.
        assert calls["n"] == 3
        err = capsys.readouterr().err
        assert "database stayed locked" in err
        assert "skill-run finish" in err
        assert "Traceback" not in err

    def test_sleep_backoff_is_bounded_and_exponential(self, monkeypatch):
        delays = []
        monkeypatch.setattr(db_lib.time, "sleep", lambda d: delays.append(d))

        def op():
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError):
            db_lib.retry_on_locked(op, retries=4, base_ms=25)
        # 25, 50, 100, 200 ms -> seconds, each capped at WRITE_RETRY_MAX_DELAY_S
        assert delays == [0.025, 0.05, 0.1, 0.2]
        assert all(d <= db_lib.WRITE_RETRY_MAX_DELAY_S for d in delays)


# ── env knob resolution (mirrors TUSK_BUSY_TIMEOUT_MS) ─────────────────


class TestWriteRetryEnvKnobs:
    def test_default_retries(self, monkeypatch):
        monkeypatch.delenv("TUSK_WRITE_RETRIES", raising=False)
        assert db_lib._write_retries() == db_lib.DEFAULT_WRITE_RETRIES

    def test_retries_env_override(self, monkeypatch):
        monkeypatch.setenv("TUSK_WRITE_RETRIES", "11")
        assert db_lib._write_retries() == 11

    def test_retries_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("TUSK_WRITE_RETRIES", "not-a-number")
        assert db_lib._write_retries() == db_lib.DEFAULT_WRITE_RETRIES

    def test_base_ms_env_override(self, monkeypatch):
        monkeypatch.setenv("TUSK_WRITE_RETRY_BASE_MS", "7")
        assert db_lib._write_retry_base_ms() == 7


# ── get_connection isolation level (issue #1143) ──────────────────────


class TestImmediateIsolation:
    def test_isolation_level_is_immediate(self, tmp_path):
        conn = db_lib.get_connection(str(tmp_path / "test.db"))
        assert conn.isolation_level == "IMMEDIATE"
        conn.close()

    def test_implicit_write_acquires_lock_up_front(self, tmp_path):
        """First write triggers BEGIN IMMEDIATE -> in_transaction is True before
        commit, confirming the RESERVED lock is held up front (not via a later
        SHARED->RESERVED promotion)."""
        conn = db_lib.get_connection(str(tmp_path / "test.db"))
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
        conn.execute("INSERT INTO t VALUES (1)")
        assert conn.in_transaction is True
        conn.commit()
        conn.close()

    def test_explicit_begin_immediate_still_works(self, tmp_path):
        """The bakeoff pattern (explicit BEGIN IMMEDIATE under a connection whose
        isolation_level is IMMEDIATE) must not raise 'transaction within a
        transaction'."""
        conn = db_lib.get_connection(str(tmp_path / "test.db"))
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t VALUES (2)")
        conn.commit()
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1
        conn.close()


# ── run_write end-to-end recovery from a held lock (issue #1143) ───────


class TestRunWriteRecovery:
    def test_run_write_recovers_after_concurrent_writer_releases(self, tmp_path, monkeypatch):
        """Regression for issue #1143: hold the write lock in another connection,
        then drive a write through run_write. With a tiny busy_timeout each
        attempt fails fast, but the whole-operation retry waits out the holder
        and the write lands instead of raising OperationalError immediately."""
        db_path = str(tmp_path / "tasks.db")
        boot = db_lib.get_connection(db_path)
        boot.execute("CREATE TABLE t (x INTEGER)")
        boot.commit()
        boot.close()

        # Tiny busy_timeout so each attempt's BEGIN IMMEDIATE fails fast and the
        # *retry* (not busy_timeout) is what absorbs the contention.
        monkeypatch.setenv("TUSK_BUSY_TIMEOUT_MS", "20")

        released = threading.Event()
        holder_ready = threading.Event()

        def holder():
            c = db_lib.get_connection(db_path)
            c.isolation_level = None
            c.execute("BEGIN IMMEDIATE")  # hold RESERVED write lock
            holder_ready.set()
            time.sleep(0.25)
            c.rollback()
            c.close()
            released.set()

        t = threading.Thread(target=holder)
        t.start()
        assert holder_ready.wait(timeout=5)

        attempts = {"n": 0}

        def do_write(conn):
            attempts["n"] += 1
            conn.execute("INSERT INTO t VALUES (1)")
            conn.commit()
            return "written"

        # base_ms small so we retry briskly across the 0.25s hold window.
        result = db_lib.run_write(db_path, do_write, base_ms=10, retries=50,
                                  label="test-write")
        t.join(timeout=5)

        assert result == "written"
        assert released.is_set()
        assert attempts["n"] >= 2  # at least one attempt failed-and-retried

        verify = db_lib.get_connection(db_path)
        assert verify.execute("SELECT count(*) FROM t").fetchone()[0] == 1
        verify.close()

    def test_run_write_reraises_when_lock_never_releases(self, tmp_path, monkeypatch):
        """If the lock is never released, run_write exhausts its budget and
        re-raises OperationalError (with the stderr diagnostic) rather than
        hanging or swallowing the failure."""
        db_path = str(tmp_path / "tasks.db")
        boot = db_lib.get_connection(db_path)
        boot.execute("CREATE TABLE t (x INTEGER)")
        boot.commit()
        boot.close()

        monkeypatch.setenv("TUSK_BUSY_TIMEOUT_MS", "10")

        holder = db_lib.get_connection(db_path)
        holder.isolation_level = None
        holder.execute("BEGIN IMMEDIATE")  # held for the duration of the test
        try:
            def do_write(conn):
                conn.execute("INSERT INTO t VALUES (1)")
                conn.commit()

            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                db_lib.run_write(db_path, do_write, base_ms=1, retries=3)
        finally:
            holder.rollback()
            holder.close()
