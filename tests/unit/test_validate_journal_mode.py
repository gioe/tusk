"""Unit tests for journal-mode drift warnings in tusk validate."""

import importlib.util
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BIN, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config_tools = _load("tusk_config_tools", "tusk-config-tools.py")


def test_missing_database_is_silent(tmp_path, capsys):
    rc = config_tools.cmd_validate_journal_mode(str(tmp_path / "missing.db"))

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""


def test_wal_database_is_silent(tmp_path, capsys):
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()

    rc = config_tools.cmd_validate_journal_mode(str(db_path))

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""


def test_non_wal_database_warns_without_failing(tmp_path, capsys):
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
        assert mode.lower() == "delete"
    finally:
        conn.close()

    rc = config_tools.cmd_validate_journal_mode(str(db_path))

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
    assert "WARNING: SQLite journal_mode is delete, expected wal" in captured.err
    assert "parallel worktree sessions" in captured.err
    assert "PRAGMA journal_mode = WAL" in captured.err
