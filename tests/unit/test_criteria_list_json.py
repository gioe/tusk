"""Unit tests for tusk criteria list — default JSON output and --pretty toggle.

Convention 32: bin/tusk-*.py emit compact JSON by default; pretty-printing
(here: the human-readable table) is opt-in via --pretty / TUSK_PRETTY=1.

Regression for issue #651: tusk criteria list previously emitted a fixed-width
table on stdout, breaking programmatic callers that try json.loads(out).
"""

import argparse
import importlib.util
import io
import json
import os
import sqlite3
from contextlib import redirect_stdout
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_criteria",
    os.path.join(REPO_ROOT, "bin", "tusk-criteria.py"),
)
criteria_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(criteria_mod)


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, summary TEXT)"
    )
    conn.execute(
        "CREATE TABLE acceptance_criteria ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  task_id INTEGER, criterion TEXT, source TEXT DEFAULT 'original',"
        "  is_completed INTEGER DEFAULT 0, is_deferred INTEGER DEFAULT 0,"
        "  deferred_reason TEXT,"
        "  criterion_type TEXT DEFAULT 'manual', verification_spec TEXT,"
        "  commit_hash TEXT, committed_at TEXT,"
        "  cost_dollars REAL, tokens_in INTEGER, tokens_out INTEGER,"
        "  skip_note TEXT, created_at TEXT"
        ")"
    )
    conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'Test task')")
    conn.execute(
        "INSERT INTO acceptance_criteria (task_id, criterion, criterion_type, is_completed, cost_dollars, commit_hash, committed_at) "
        "VALUES (1, 'first criterion', 'manual', 1, 0.05, 'abc1234', '2026-05-04T10:00:00')"
    )
    conn.execute(
        "INSERT INTO acceptance_criteria (task_id, criterion, criterion_type) "
        "VALUES (1, 'second criterion', 'test')"
    )
    conn.commit()
    return conn


def _run_list(task_id, *, pretty_env=None):
    conn = _make_conn()
    args = argparse.Namespace(task_id=task_id)
    out = io.StringIO()
    env = {}
    if pretty_env is not None:
        env["TUSK_PRETTY"] = pretty_env
    with patch.dict(os.environ, env, clear=False), \
         patch.object(criteria_mod, "get_connection", return_value=conn), \
         redirect_stdout(out):
        if pretty_env is None:
            os.environ.pop("TUSK_PRETTY", None)
        rc = criteria_mod.cmd_list(args, db_path="ignored", config={})
    return rc, out.getvalue()


class TestCriteriaListJson:
    def test_default_emits_parseable_json_array(self):
        rc, stdout = _run_list(1)
        assert rc == 0
        data = json.loads(stdout)
        assert isinstance(data, list)
        assert len(data) == 2

    def test_json_rows_contain_table_columns(self):
        rc, stdout = _run_list(1)
        assert rc == 0
        data = json.loads(stdout)
        first = data[0]
        for key in (
            "id", "criterion", "source", "is_completed",
            "criterion_type", "cost_dollars", "commit_hash", "committed_at",
        ):
            assert key in first, f"missing key {key!r} in JSON row"
        assert first["id"] == 1
        assert first["criterion"] == "first criterion"
        assert first["is_completed"] == 1
        assert first["commit_hash"] == "abc1234"

    def test_json_default_is_compact(self):
        rc, stdout = _run_list(1)
        assert rc == 0
        # Compact JSON has no newlines between records and no spaces after separators.
        assert "\n" not in stdout.strip()
        assert ", " not in stdout
        assert ": " not in stdout

    def test_empty_criteria_returns_empty_array(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, summary TEXT)")
        conn.execute(
            "CREATE TABLE acceptance_criteria ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  task_id INTEGER, criterion TEXT, source TEXT,"
            "  is_completed INTEGER, is_deferred INTEGER, deferred_reason TEXT,"
            "  cost_dollars REAL, tokens_in INTEGER, tokens_out INTEGER,"
            "  criterion_type TEXT, verification_spec TEXT,"
            "  commit_hash TEXT, committed_at TEXT,"
            "  skip_note TEXT, created_at TEXT"
            ")"
        )
        conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'no crit')")
        conn.commit()
        args = argparse.Namespace(task_id=1)
        out = io.StringIO()
        env_no_pretty = {k: v for k, v in os.environ.items() if k != "TUSK_PRETTY"}
        with patch.dict(os.environ, env_no_pretty, clear=True), \
             patch.object(criteria_mod, "get_connection", return_value=conn), \
             redirect_stdout(out):
            rc = criteria_mod.cmd_list(args, db_path="ignored", config={})
        assert rc == 0
        assert json.loads(out.getvalue()) == []

    def test_task_not_found_returns_2(self):
        conn = _make_conn()
        args = argparse.Namespace(task_id=9999)
        with patch.object(criteria_mod, "get_connection", return_value=conn):
            rc = criteria_mod.cmd_list(args, db_path="ignored", config={})
        assert rc == 2

    def test_pretty_env_renders_table(self):
        rc, stdout = _run_list(1, pretty_env="1")
        assert rc == 0
        assert "Acceptance criteria for task #1: Test task" in stdout
        assert "ID" in stdout and "Done" in stdout and "Criterion" in stdout
        assert "first criterion" in stdout

    def test_pretty_env_truthy_values(self):
        for value in ("1", "true", "yes", "on", "TRUE", "Yes"):
            rc, stdout = _run_list(1, pretty_env=value)
            assert rc == 0, f"rc != 0 for TUSK_PRETTY={value!r}"
            assert "Acceptance criteria for task #1" in stdout, \
                f"table not rendered for TUSK_PRETTY={value!r}"

    def test_pretty_env_falsy_emits_json(self):
        for value in ("", "0", "false", "no"):
            rc, stdout = _run_list(1, pretty_env=value)
            assert rc == 0, f"rc != 0 for TUSK_PRETTY={value!r}"
            data = json.loads(stdout)
            assert isinstance(data, list), \
                f"expected JSON array for TUSK_PRETTY={value!r}"
