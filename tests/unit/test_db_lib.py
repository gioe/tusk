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
