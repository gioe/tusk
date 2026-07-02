"""Regression tests for transient SQLITE_BUSY read retries."""

import importlib.util
import json
import os
import sqlite3
import threading
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_db_spec = importlib.util.spec_from_file_location(
    "tusk_db_lib",
    os.path.join(BIN, "tusk-db-lib.py"),
)
db_lib = importlib.util.module_from_spec(_db_spec)
_db_spec.loader.exec_module(db_lib)

_setup_spec = importlib.util.spec_from_file_location(
    "tusk_setup",
    os.path.join(BIN, "tusk-setup.py"),
)
setup_mod = importlib.util.module_from_spec(_setup_spec)
_setup_spec.loader.exec_module(setup_mod)

_retro_spec = importlib.util.spec_from_file_location(
    "tusk_retro_signals",
    os.path.join(BIN, "tusk-retro-signals.py"),
)
retro_mod = importlib.util.module_from_spec(_retro_spec)
_retro_spec.loader.exec_module(retro_mod)


def _make_tasks_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tasks ("
        "id INTEGER PRIMARY KEY, summary TEXT, status TEXT, priority TEXT, "
        "domain TEXT, assignee TEXT, complexity TEXT, task_type TEXT, priority_score INTEGER"
        ")"
    )
    conn.execute(
        "INSERT INTO tasks (id, summary, status, priority, priority_score) "
        "VALUES (1, 'open task', 'To Do', 'High', 10)"
    )
    conn.commit()
    conn.close()


def _make_retro_db(db_path):
    from tests.unit.test_retro_signals import _SCHEMA

    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO tasks (id, summary, status, complexity) VALUES (1, 'task', 'Done', 'M')"
    )
    conn.commit()
    conn.close()


class TestReadRetry:
    def test_run_read_reraises_when_lock_never_releases(self, tmp_path, monkeypatch, capsys):
        db_path = str(tmp_path / "tasks.db")
        _make_tasks_db(db_path)
        monkeypatch.setattr(db_lib.time, "sleep", lambda *_: None)

        def read_op(_conn):
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            db_lib.run_read(db_path, read_op, base_ms=1, retries=2, label="setup")

        err = capsys.readouterr().err
        assert "database stayed locked" in err
        assert "setup" in err
        assert "Traceback" not in err

    def test_setup_retries_transient_locked_read(self, tmp_path, monkeypatch, capsys):
        db_path = str(tmp_path / "tasks.db")
        config_path = tmp_path / "config.json"
        _make_tasks_db(db_path)
        config_path.write_text(json.dumps({"domains": [], "task_types": []}), encoding="utf-8")
        monkeypatch.setenv("TUSK_BUSY_TIMEOUT_MS", "10")
        monkeypatch.setenv("TUSK_WRITE_RETRIES", "50")
        monkeypatch.setenv("TUSK_WRITE_RETRY_BASE_MS", "5")

        holder_ready = threading.Event()

        def holder():
            conn = sqlite3.connect(db_path)
            conn.execute("BEGIN EXCLUSIVE")
            holder_ready.set()
            time.sleep(0.2)
            conn.rollback()
            conn.close()

        thread = threading.Thread(target=holder)
        thread.start()
        assert holder_ready.wait(timeout=5)

        rc = setup_mod.main([db_path, str(config_path)])
        thread.join(timeout=5)

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["backlog"][0]["summary"] == "open task"

    def test_retro_signals_retries_transient_locked_read(self, tmp_path, monkeypatch, capsys):
        db_path = str(tmp_path / "tasks.db")
        _make_retro_db(db_path)
        monkeypatch.setenv("TUSK_BUSY_TIMEOUT_MS", "10")
        monkeypatch.setenv("TUSK_WRITE_RETRIES", "50")
        monkeypatch.setenv("TUSK_WRITE_RETRY_BASE_MS", "5")

        holder_ready = threading.Event()

        def holder():
            conn = sqlite3.connect(db_path)
            conn.execute("BEGIN EXCLUSIVE")
            holder_ready.set()
            time.sleep(0.2)
            conn.rollback()
            conn.close()

        thread = threading.Thread(target=holder)
        thread.start()
        assert holder_ready.wait(timeout=5)

        rc = retro_mod.main([db_path, "fake.json", "1"])
        thread.join(timeout=5)

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["task_id"] == 1
        assert payload["context_health"]["missing_entry_points"] is True
