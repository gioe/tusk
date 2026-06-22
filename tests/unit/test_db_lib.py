"""Unit tests for get_connection() and load_config() in tusk-db-lib.py."""

import importlib.util
import json
import os
import sqlite3

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_db_lib",
    os.path.join(REPO_ROOT, "bin", "tusk-db-lib.py"),
)
db_lib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(db_lib)


# ── get_connection ────────────────────────────────────────────────────


class TestGetConnection:
    def test_returns_sqlite_connection(self, tmp_path):
        conn = db_lib.get_connection(str(tmp_path / "test.db"))
        assert isinstance(conn, sqlite3.Connection)
        conn.close()

    def test_row_factory_is_set(self, tmp_path):
        conn = db_lib.get_connection(str(tmp_path / "test.db"))
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'hello')")
        row = conn.execute("SELECT a, b FROM t").fetchone()
        # sqlite3.Row supports column name access
        assert row["a"] == 1
        assert row["b"] == "hello"
        conn.close()

    def test_foreign_keys_enabled(self, tmp_path):
        conn = db_lib.get_connection(str(tmp_path / "test.db"))
        result = conn.execute("PRAGMA foreign_keys").fetchone()
        # Row index 0 is the foreign_keys value
        assert result[0] == 1
        conn.close()

    def test_busy_timeout_default_applied(self, tmp_path, monkeypatch):
        # Issue #946: concurrent writers must wait on a lock rather than fail
        # instantly with "database is locked".
        monkeypatch.delenv("TUSK_BUSY_TIMEOUT_MS", raising=False)
        conn = db_lib.get_connection(str(tmp_path / "test.db"))
        result = conn.execute("PRAGMA busy_timeout").fetchone()
        assert result[0] == db_lib.DEFAULT_BUSY_TIMEOUT_MS
        assert db_lib.DEFAULT_BUSY_TIMEOUT_MS > 0
        conn.close()

    def test_busy_timeout_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TUSK_BUSY_TIMEOUT_MS", "1234")
        conn = db_lib.get_connection(str(tmp_path / "test.db"))
        result = conn.execute("PRAGMA busy_timeout").fetchone()
        assert result[0] == 1234
        conn.close()

    def test_busy_timeout_invalid_env_falls_back_to_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TUSK_BUSY_TIMEOUT_MS", "not-a-number")
        conn = db_lib.get_connection(str(tmp_path / "test.db"))
        result = conn.execute("PRAGMA busy_timeout").fetchone()
        assert result[0] == db_lib.DEFAULT_BUSY_TIMEOUT_MS
        conn.close()

    def test_creates_db_file(self, tmp_path):
        db_file = tmp_path / "new.db"
        assert not db_file.exists()
        conn = db_lib.get_connection(str(db_file))
        conn.close()
        assert db_file.exists()

    def test_connection_is_usable(self, tmp_path):
        conn = db_lib.get_connection(str(tmp_path / "test.db"))
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO items (name) VALUES (?)", ("tusk",))
        conn.commit()
        row = conn.execute("SELECT name FROM items WHERE id = 1").fetchone()
        assert row["name"] == "tusk"
        conn.close()

    def test_missing_parent_dir_exits_cleanly_with_diagnostic(self, tmp_path, capsys):
        """Issue #1126: opening a DB whose parent dir does not exist (i.e. not
        inside an initialized tusk project) must emit an actionable one-line
        diagnostic and exit non-zero — not a raw OperationalError traceback.
        """
        missing = tmp_path / "no-such-dir" / "tasks.db"
        assert not missing.parent.exists()

        with pytest.raises(SystemExit) as exc_info:
            db_lib.get_connection(str(missing))
        assert exc_info.value.code != 0

        err = capsys.readouterr().err
        assert "could not locate a tusk database" in err
        assert str(missing) in err
        assert "tusk init" in err
        # The raw exception name must NOT leak — the whole point is no traceback.
        assert "OperationalError" not in err
        assert "Traceback" not in err

    def test_genuine_open_error_is_reraised_not_swallowed(self, tmp_path, monkeypatch):
        """A failure to open a DB whose parent dir DOES exist (e.g. real
        corruption/permission error) must propagate as OperationalError, not be
        masked by the issue #1126 missing-project diagnostic.
        """
        def boom(*a, **k):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(db_lib.sqlite3, "connect", boom)
        # Parent dir exists, so the guard must re-raise rather than exit.
        with pytest.raises(sqlite3.OperationalError):
            db_lib.get_connection(str(tmp_path / "tasks.db"))


# ── load_config ───────────────────────────────────────────────────────


class TestLoadConfig:
    def test_returns_dict(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text('{"key": "value"}')
        result = db_lib.load_config(str(cfg))
        assert isinstance(result, dict)

    def test_parses_simple_json(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text('{"domains": ["cli", "db"], "version": 1}')
        result = db_lib.load_config(str(cfg))
        assert result["domains"] == ["cli", "db"]
        assert result["version"] == 1

    def test_parses_nested_json(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text('{"review": {"mode": "ai_only", "max_passes": 3}}')
        result = db_lib.load_config(str(cfg))
        assert result["review"]["mode"] == "ai_only"
        assert result["review"]["max_passes"] == 3

    def test_loads_real_config(self):
        config_path = os.path.join(REPO_ROOT, "config.default.json")
        result = db_lib.load_config(config_path)
        assert isinstance(result, dict)
        # config.default.json always has these top-level keys
        assert "domains" in result
        assert "priorities" in result

    def test_raises_on_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            db_lib.load_config(str(tmp_path / "nonexistent.json"))

    def test_raises_on_invalid_json(self, tmp_path):
        cfg = tmp_path / "bad.json"
        cfg.write_text("{not valid json}")
        with pytest.raises(json.JSONDecodeError):
            db_lib.load_config(str(cfg))
