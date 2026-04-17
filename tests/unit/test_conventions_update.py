"""Unit tests for tusk-conventions.py cmd_update function.

Uses an in-memory SQLite DB — no filesystem or subprocess required.
"""

import importlib.util
import io
import os
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_conventions",
    os.path.join(REPO_ROOT, "bin", "tusk-conventions.py"),
)
conventions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(conventions)


def make_db(*rows: tuple[int, str, str]) -> sqlite3.Connection:
    """Return an in-memory connection with a conventions table.

    rows: sequence of (id, text, topics) tuples.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE conventions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            source_skill TEXT,
            topics TEXT,
            violation_count INTEGER DEFAULT 0
        )"""
    )
    for cid, text, topics in rows:
        conn.execute(
            "INSERT INTO conventions (id, text, topics) VALUES (?, ?, ?)",
            (cid, text, topics),
        )
    conn.commit()
    return conn


class _NonClosingConn:
    """Wraps a sqlite3.Connection but makes close() a no-op.

    cmd_update calls conn.close() in a finally block, which would invalidate
    the in-memory DB before we can assert on it. This wrapper prevents that.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def close(self):
        pass  # intentionally a no-op

    def __getattr__(self, name):
        return getattr(self._conn, name)


def capture(fn, args, conn: sqlite3.Connection, config=None):
    """Call fn(args, db_path, config), capturing stdout/stderr.

    Monkey-patches get_connection to return a non-closing wrapper around conn
    so that the caller can still query the DB after the function returns.
    Returns (rc, stdout, stderr).
    """
    if config is None:
        config = {}
    out, err = io.StringIO(), io.StringIO()
    wrapper = _NonClosingConn(conn)
    orig = conventions.get_connection
    conventions.get_connection = lambda _path: wrapper
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = fn(args, ":memory:", config)
    finally:
        conventions.get_connection = orig
    return rc, out.getvalue(), err.getvalue()


class TestCmdUpdate:
    def test_update_text_only(self):
        conn = make_db((1, "original text", "cli,git"))
        args = SimpleNamespace(id=1, text="updated text", topics=None)
        rc, out, _ = capture(conventions.cmd_update, args, conn)
        assert rc == 0
        # Non-TTY stdout (StringIO) suppresses the "Updated convention #N" line;
        # skills rely on exit code, not human-readable output.
        assert out == ""
        row = conn.execute("SELECT text, topics FROM conventions WHERE id=1").fetchone()
        assert row["text"] == "updated text"
        assert row["topics"] == "cli,git"  # unchanged

    def test_update_topics_only(self):
        conn = make_db((1, "some text", "cli"))
        args = SimpleNamespace(id=1, text=None, topics="cli,sql,git")
        rc, out, _ = capture(conventions.cmd_update, args, conn)
        assert rc == 0
        row = conn.execute("SELECT text, topics FROM conventions WHERE id=1").fetchone()
        assert row["text"] == "some text"  # unchanged
        assert row["topics"] == "cli,sql,git"

    def test_update_both_text_and_topics(self):
        conn = make_db((1, "old", "old_topic"))
        args = SimpleNamespace(id=1, text="new text", topics="new,topics")
        rc, out, _ = capture(conventions.cmd_update, args, conn)
        assert rc == 0
        row = conn.execute("SELECT text, topics FROM conventions WHERE id=1").fetchone()
        assert row["text"] == "new text"
        assert row["topics"] == "new,topics"

    def test_topics_whitespace_is_normalized(self):
        conn = make_db((1, "text", "old"))
        args = SimpleNamespace(id=1, text=None, topics=" cli , sql , git ")
        rc, _, _ = capture(conventions.cmd_update, args, conn)
        assert rc == 0
        row = conn.execute("SELECT topics FROM conventions WHERE id=1").fetchone()
        assert row["topics"] == "cli,sql,git"

    def test_nonexistent_id_returns_2(self):
        conn = make_db((1, "text", "cli"))
        args = SimpleNamespace(id=99, text="new text", topics=None)
        rc, _, err = capture(conventions.cmd_update, args, conn)
        assert rc == 2
        assert "not found" in err

    def test_no_flags_returns_1(self):
        conn = make_db((1, "text", "cli"))
        args = SimpleNamespace(id=1, text=None, topics=None)
        rc, _, err = capture(conventions.cmd_update, args, conn)
        assert rc == 1
        assert "--text" in err or "required" in err

    def test_updated_convention_visible_in_list(self):
        conn = make_db((1, "original", "cli"))
        args = SimpleNamespace(id=1, text="brand new", topics=None)
        capture(conventions.cmd_update, args, conn)
        row = conn.execute("SELECT text FROM conventions WHERE id=1").fetchone()
        assert row["text"] == "brand new"
