"""Unit tests for tusk deps list / tusk review list — default JSON output and --pretty toggle.

Convention 32: bin/tusk-*.py emit compact JSON by default; pretty-printing
(here: the human-readable header + table / nested block) is opt-in via
--pretty / TUSK_PRETTY=1.

Regression for issue #652: tusk deps list and tusk review list previously
emitted human-readable text on stdout, breaking programmatic callers that try
json.loads(out).
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


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO_ROOT, "bin", filename)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


deps_mod = _load_module("tusk_deps", "tusk-deps.py")
review_mod = _load_module("tusk_review", "tusk-review.py")


# ── deps list ────────────────────────────────────────────────────────


def _make_deps_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks ("
        "  id INTEGER PRIMARY KEY, summary TEXT, status TEXT, priority TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE task_dependencies ("
        "  task_id INTEGER, depends_on_id INTEGER, relationship_type TEXT"
        ")"
    )
    conn.execute("INSERT INTO tasks (id, summary, status, priority) VALUES (1, 'parent', 'To Do', 'Medium')")
    conn.execute("INSERT INTO tasks (id, summary, status, priority) VALUES (2, 'dep one', 'Done', 'High')")
    conn.execute("INSERT INTO tasks (id, summary, status, priority) VALUES (3, 'dep two', 'In Progress', 'Low')")
    conn.execute("INSERT INTO task_dependencies VALUES (1, 2, 'blocks')")
    conn.execute("INSERT INTO task_dependencies VALUES (1, 3, 'contingent')")
    conn.commit()
    return conn


def _run_deps_list(task_id, *, pretty_env=None):
    conn = _make_deps_conn()
    out = io.StringIO()
    env = {}
    if pretty_env is not None:
        env["TUSK_PRETTY"] = pretty_env
    with patch.dict(os.environ, env, clear=False), redirect_stdout(out):
        if pretty_env is None:
            os.environ.pop("TUSK_PRETTY", None)
        rc = deps_mod.list_dependencies(conn, task_id, json_output=False)
    return rc, out.getvalue()


class TestDepsListJson:
    def test_default_emits_parseable_json_array(self):
        rc, stdout = _run_deps_list(1)
        assert rc == 0
        data = json.loads(stdout)
        assert isinstance(data, list)
        assert len(data) == 2

    def test_json_rows_contain_expected_keys(self):
        rc, stdout = _run_deps_list(1)
        assert rc == 0
        data = json.loads(stdout)
        first = data[0]
        for key in ("id", "summary", "status", "priority", "relationship_type"):
            assert key in first, f"missing key {key!r} in JSON row"
        assert first["id"] == 2
        assert first["relationship_type"] == "blocks"
        assert data[1]["relationship_type"] == "contingent"

    def test_json_default_is_compact(self):
        rc, stdout = _run_deps_list(1)
        assert rc == 0
        assert "\n" not in stdout.strip()
        assert ", " not in stdout
        assert ": " not in stdout

    def test_empty_deps_returns_empty_array(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, summary TEXT, status TEXT, priority TEXT)")
        conn.execute("CREATE TABLE task_dependencies (task_id INTEGER, depends_on_id INTEGER, relationship_type TEXT)")
        conn.execute("INSERT INTO tasks (id, summary, status, priority) VALUES (1, 'lonely', 'To Do', 'Medium')")
        conn.commit()
        out = io.StringIO()
        env_no_pretty = {k: v for k, v in os.environ.items() if k != "TUSK_PRETTY"}
        with patch.dict(os.environ, env_no_pretty, clear=True), redirect_stdout(out):
            rc = deps_mod.list_dependencies(conn, 1, json_output=False)
        assert rc == 0
        assert json.loads(out.getvalue()) == []

    def test_task_not_found_returns_1(self):
        conn = _make_deps_conn()
        rc = deps_mod.list_dependencies(conn, 9999, json_output=False)
        assert rc == 1

    def test_pretty_env_renders_table(self):
        rc, stdout = _run_deps_list(1, pretty_env="1")
        assert rc == 0
        assert "Dependencies for Task 1: parent" in stdout
        assert "Status" in stdout and "Priority" in stdout and "Type" in stdout
        assert "dep one" in stdout
        assert "dep two" in stdout

    def test_pretty_env_truthy_values(self):
        for value in ("1", "true", "yes", "on", "TRUE", "Yes"):
            rc, stdout = _run_deps_list(1, pretty_env=value)
            assert rc == 0, f"rc != 0 for TUSK_PRETTY={value!r}"
            assert "Dependencies for Task 1" in stdout, (
                f"table not rendered for TUSK_PRETTY={value!r}"
            )

    def test_pretty_env_falsy_emits_json(self):
        for value in ("", "0", "false", "no"):
            rc, stdout = _run_deps_list(1, pretty_env=value)
            assert rc == 0, f"rc != 0 for TUSK_PRETTY={value!r}"
            data = json.loads(stdout)
            assert isinstance(data, list), (
                f"expected JSON array for TUSK_PRETTY={value!r}"
            )

    def test_explicit_json_flag_still_works(self):
        conn = _make_deps_conn()
        out = io.StringIO()
        with patch.dict(os.environ, {"TUSK_PRETTY": "1"}, clear=False), redirect_stdout(out):
            rc = deps_mod.list_dependencies(conn, 1, json_output=True)
        assert rc == 0
        data = json.loads(out.getvalue())
        assert isinstance(data, list)
        assert len(data) == 2


# ── review list ──────────────────────────────────────────────────────


def _make_review_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, summary TEXT)")
    conn.execute(
        "CREATE TABLE code_reviews ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  task_id INTEGER, reviewer TEXT, status TEXT, review_pass INTEGER,"
        "  created_at TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE review_comments ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  review_id INTEGER, file_path TEXT, line_start INTEGER, line_end INTEGER,"
        "  category TEXT, severity TEXT, comment TEXT, resolution TEXT,"
        "  resolution_note TEXT"
        ")"
    )
    conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'Test task')")
    conn.execute(
        "INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass, created_at) "
        "VALUES (1, 1, 'reviewer-bot', 'approved', 1, '2026-05-04T10:00:00')"
    )
    conn.execute(
        "INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass, created_at) "
        "VALUES (2, 1, NULL, 'changes_requested', 1, '2026-05-04T11:00:00')"
    )
    conn.execute(
        "INSERT INTO review_comments (id, review_id, file_path, line_start, category, severity, comment, resolution) "
        "VALUES (10, 2, 'foo.py', 42, 'must_fix', 'critical', 'fix this', NULL)"
    )
    conn.commit()
    return conn


def _run_review_list(task_id, *, pretty_env=None):
    conn = _make_review_conn()
    args = argparse.Namespace(task_id=task_id)
    out = io.StringIO()
    env = {}
    if pretty_env is not None:
        env["TUSK_PRETTY"] = pretty_env
    with patch.dict(os.environ, env, clear=False), \
         patch.object(review_mod, "get_connection", return_value=conn), \
         redirect_stdout(out):
        if pretty_env is None:
            os.environ.pop("TUSK_PRETTY", None)
        rc = review_mod.cmd_list(args, db_path="ignored")
    return rc, out.getvalue()


class TestReviewListJson:
    def test_default_emits_parseable_json_array(self):
        rc, stdout = _run_review_list(1)
        assert rc == 0
        data = json.loads(stdout)
        assert isinstance(data, list)
        assert len(data) == 2

    def test_json_rows_contain_expected_keys_and_nested_comments(self):
        rc, stdout = _run_review_list(1)
        assert rc == 0
        data = json.loads(stdout)
        first = data[0]
        for key in ("id", "reviewer", "status", "review_pass", "created_at", "comments"):
            assert key in first, f"missing key {key!r} in JSON row"
        assert first["id"] == 1
        assert first["reviewer"] == "reviewer-bot"
        assert first["comments"] == []
        second = data[1]
        assert second["reviewer"] is None
        assert len(second["comments"]) == 1
        c = second["comments"][0]
        assert c["id"] == 10
        assert c["file_path"] == "foo.py"
        assert c["category"] == "must_fix"
        assert c["severity"] == "critical"

    def test_json_default_is_compact(self):
        rc, stdout = _run_review_list(1)
        assert rc == 0
        assert "\n" not in stdout.strip()
        assert ", " not in stdout
        assert ": " not in stdout

    def test_empty_reviews_returns_empty_array(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, summary TEXT)")
        conn.execute(
            "CREATE TABLE code_reviews ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  task_id INTEGER, reviewer TEXT, status TEXT, review_pass INTEGER,"
            "  created_at TEXT"
            ")"
        )
        conn.execute(
            "CREATE TABLE review_comments ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  review_id INTEGER, file_path TEXT, line_start INTEGER, line_end INTEGER,"
            "  category TEXT, severity TEXT, comment TEXT, resolution TEXT"
            ")"
        )
        conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'no reviews')")
        conn.commit()
        args = argparse.Namespace(task_id=1)
        out = io.StringIO()
        env_no_pretty = {k: v for k, v in os.environ.items() if k != "TUSK_PRETTY"}
        with patch.dict(os.environ, env_no_pretty, clear=True), \
             patch.object(review_mod, "get_connection", return_value=conn), \
             redirect_stdout(out):
            rc = review_mod.cmd_list(args, db_path="ignored")
        assert rc == 0
        assert json.loads(out.getvalue()) == []

    def test_task_not_found_returns_2(self):
        conn = _make_review_conn()
        args = argparse.Namespace(task_id=9999)
        with patch.object(review_mod, "get_connection", return_value=conn):
            rc = review_mod.cmd_list(args, db_path="ignored")
        assert rc == 2

    def test_pretty_env_renders_block(self):
        rc, stdout = _run_review_list(1, pretty_env="1")
        assert rc == 0
        assert "Reviews for task #1: Test task" in stdout
        assert "Review #1" in stdout
        assert "Review #2" in stdout
        assert "fix this" in stdout

    def test_pretty_env_truthy_values(self):
        for value in ("1", "true", "yes", "on", "TRUE", "Yes"):
            rc, stdout = _run_review_list(1, pretty_env=value)
            assert rc == 0, f"rc != 0 for TUSK_PRETTY={value!r}"
            assert "Reviews for task #1" in stdout, (
                f"block not rendered for TUSK_PRETTY={value!r}"
            )

    def test_pretty_env_falsy_emits_json(self):
        for value in ("", "0", "false", "no"):
            rc, stdout = _run_review_list(1, pretty_env=value)
            assert rc == 0, f"rc != 0 for TUSK_PRETTY={value!r}"
            data = json.loads(stdout)
            assert isinstance(data, list), (
                f"expected JSON array for TUSK_PRETTY={value!r}"
            )
