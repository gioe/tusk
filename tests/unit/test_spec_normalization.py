"""Unit tests for verification_spec write-boundary normalization (issue #1045).

Empty or whitespace-only specs passed to `tusk criteria add` / `tusk criteria
update` must be stored as SQL NULL, never as '' — a zero-length string counts
as "has a spec" for downstream consumers (lint Rule 10, criteria done) and
produced blocking lint violations from unrelated tasks' rows at merge time.

Uses an in-memory SQLite DB — no filesystem or subprocess required.
"""

import argparse
import importlib.util
import io
import os
import sqlite3
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_criteria",
    os.path.join(REPO_ROOT, "bin", "tusk-criteria.py"),
)
criteria_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(criteria_mod)


class _NoCloseConn:
    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


def _make_db():
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
        "  deferred_reason TEXT, criterion_type TEXT DEFAULT 'manual',"
        "  verification_spec TEXT,"
        "  created_at TEXT DEFAULT (datetime('now')),"
        "  updated_at TEXT DEFAULT (datetime('now')))"
    )
    conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'spec normalization host')")
    conn.commit()
    return conn


def _add_args(spec, ctype="manual"):
    return argparse.Namespace(
        task_id=1, text="normalize me", source="original", type=ctype, spec=spec
    )


def _stored_spec(conn, criterion_text="normalize me"):
    row = conn.execute(
        "SELECT verification_spec FROM acceptance_criteria WHERE criterion = ?",
        (criterion_text,),
    ).fetchone()
    assert row is not None
    return row["verification_spec"]


class TestNormalizeSpecHelper:
    def test_none_stays_none(self):
        assert criteria_mod._normalize_spec(None) is None

    def test_empty_string_becomes_none(self):
        assert criteria_mod._normalize_spec("") is None

    def test_whitespace_only_becomes_none(self):
        assert criteria_mod._normalize_spec("   \t\n") is None

    def test_real_spec_passes_through_unchanged(self):
        assert criteria_mod._normalize_spec("pytest -q") == "pytest -q"


class TestCmdAddNormalization:
    def _run_add(self, conn, spec, ctype="manual"):
        out = io.StringIO()
        with patch.object(criteria_mod, "get_connection", return_value=_NoCloseConn(conn)):
            with redirect_stdout(out):
                rc = criteria_mod.cmd_add(_add_args(spec, ctype), ":memory:", {})
        return rc

    def test_empty_spec_stored_as_null(self):
        conn = _make_db()
        assert self._run_add(conn, "") == 0
        assert _stored_spec(conn) is None

    def test_whitespace_spec_stored_as_null(self):
        conn = _make_db()
        assert self._run_add(conn, "   ") == 0
        assert _stored_spec(conn) is None

    def test_real_spec_stored_verbatim(self):
        conn = _make_db()
        assert self._run_add(conn, "pytest -q", ctype="test") == 0
        assert _stored_spec(conn) == "pytest -q"

    def test_whitespace_spec_rejected_for_spec_required_type(self):
        # Pre-fix, '   ' was truthy and slipped past the required-spec guard.
        conn = _make_db()
        assert self._run_add(conn, "   ", ctype="test") == 2


class TestNormalizeUpdateSpec:
    def test_blank_update_spec_clears_to_null(self):
        assert criteria_mod._normalize_update_spec("") == (True, None)

    def test_whitespace_update_spec_clears_to_null(self):
        assert criteria_mod._normalize_update_spec("  ") == (True, None)

    def test_null_sentinel_still_clears(self):
        assert criteria_mod._normalize_update_spec("NULL") == (True, None)

    def test_absent_means_unchanged(self):
        assert criteria_mod._normalize_update_spec(None) == (False, None)

    def test_real_spec_passes_through(self):
        assert criteria_mod._normalize_update_spec("test -f x") == (True, "test -f x")
