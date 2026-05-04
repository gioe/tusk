"""Unit tests for tusk deps dependents / blocked / ready / all — default JSON output and --pretty toggle.

Convention 32: bin/tusk-*.py emit compact JSON by default; pretty-printing
(here: the human-readable headers + column-aligned tables) is opt-in via
--pretty / TUSK_PRETTY=1.

Regression for issue #653: the four sibling subcommands previously emitted
human-readable text on stdout, breaking programmatic callers that try
json.loads(out). Mirrors TASK-299's test_deps_review_list_json structure.
"""

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


def _make_conn():
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
    # Tasks: 1 (parent, blocked) -> deps 2 (Done), 3 (To Do)
    #         4 (no deps, ready)
    conn.execute("INSERT INTO tasks (id, summary, status, priority) VALUES (1, 'parent', 'To Do', 'Medium')")
    conn.execute("INSERT INTO tasks (id, summary, status, priority) VALUES (2, 'dep done', 'Done', 'High')")
    conn.execute("INSERT INTO tasks (id, summary, status, priority) VALUES (3, 'dep waiting', 'To Do', 'Low')")
    conn.execute("INSERT INTO tasks (id, summary, status, priority) VALUES (4, 'standalone', 'To Do', 'Medium')")
    conn.execute("INSERT INTO task_dependencies VALUES (1, 2, 'blocks')")
    conn.execute("INSERT INTO task_dependencies VALUES (1, 3, 'contingent')")
    # v_ready_tasks view: tasks whose status != Done with no incomplete deps
    conn.execute("""
        CREATE VIEW v_ready_tasks AS
        SELECT t.id, t.summary, t.status, t.priority
        FROM tasks t
        WHERE t.status <> 'Done'
          AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks dep ON d.depends_on_id = dep.id
            WHERE d.task_id = t.id AND dep.status <> 'Done'
          )
    """)
    conn.commit()
    return conn


def _run(fn, *args, pretty_env=None):
    conn = _make_conn()
    out = io.StringIO()
    env = {}
    if pretty_env is not None:
        env["TUSK_PRETTY"] = pretty_env
    with patch.dict(os.environ, env, clear=False), redirect_stdout(out):
        if pretty_env is None:
            os.environ.pop("TUSK_PRETTY", None)
        rc = fn(conn, *args)
    return rc, out.getvalue()


# ── deps dependents ──────────────────────────────────────────────────


class TestDepsDependentsJson:
    def test_default_emits_parseable_json_array(self):
        # task 2 has dependent task 1 (blocks); task 3 has dependent task 1 (contingent)
        rc, stdout = _run(deps_mod.list_dependents, 2, False)
        assert rc == 0
        data = json.loads(stdout)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == 1
        assert data[0]["relationship_type"] == "blocks"

    def test_json_default_is_compact(self):
        rc, stdout = _run(deps_mod.list_dependents, 2, False)
        assert rc == 0
        assert "\n" not in stdout.strip()
        assert ", " not in stdout
        assert ": " not in stdout

    def test_empty_returns_empty_array(self):
        rc, stdout = _run(deps_mod.list_dependents, 4, False)
        assert rc == 0
        assert json.loads(stdout) == []

    def test_task_not_found_returns_1(self):
        conn = _make_conn()
        rc = deps_mod.list_dependents(conn, 9999, False)
        assert rc == 1

    def test_pretty_env_renders_table(self):
        rc, stdout = _run(deps_mod.list_dependents, 2, False, pretty_env="1")
        assert rc == 0
        assert "Tasks that depend on Task 2" in stdout
        assert "parent" in stdout

    def test_pretty_env_truthy_values(self):
        for value in ("1", "true", "yes", "on", "TRUE", "Yes"):
            rc, stdout = _run(deps_mod.list_dependents, 2, False, pretty_env=value)
            assert rc == 0, f"rc != 0 for TUSK_PRETTY={value!r}"
            assert "Tasks that depend on Task 2" in stdout, (
                f"table not rendered for TUSK_PRETTY={value!r}"
            )

    def test_pretty_env_falsy_emits_json(self):
        for value in ("", "0", "false", "no"):
            rc, stdout = _run(deps_mod.list_dependents, 2, False, pretty_env=value)
            assert rc == 0, f"rc != 0 for TUSK_PRETTY={value!r}"
            data = json.loads(stdout)
            assert isinstance(data, list), (
                f"expected JSON array for TUSK_PRETTY={value!r}"
            )

    def test_explicit_json_flag_still_works(self):
        conn = _make_conn()
        out = io.StringIO()
        with patch.dict(os.environ, {"TUSK_PRETTY": "1"}, clear=False), redirect_stdout(out):
            rc = deps_mod.list_dependents(conn, 2, True)
        assert rc == 0
        data = json.loads(out.getvalue())
        assert isinstance(data, list) and len(data) == 1


# ── deps blocked ─────────────────────────────────────────────────────


class TestDepsBlockedJson:
    def test_default_emits_parseable_json_array(self):
        rc, stdout = _run(deps_mod.show_blocked, False)
        assert rc == 0
        data = json.loads(stdout)
        assert isinstance(data, list)
        # task 1 is blocked by dep 3 (To Do)
        assert any(t["id"] == 1 for t in data)
        first = next(t for t in data if t["id"] == 1)
        for key in ("id", "summary", "status", "priority", "blocking_count", "total_deps"):
            assert key in first

    def test_json_default_is_compact(self):
        rc, stdout = _run(deps_mod.show_blocked, False)
        assert rc == 0
        assert "\n" not in stdout.strip()
        assert ", " not in stdout
        assert ": " not in stdout

    def test_empty_returns_empty_array(self):
        # Fresh DB with no blocked tasks
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, summary TEXT, status TEXT, priority TEXT)")
        conn.execute("CREATE TABLE task_dependencies (task_id INTEGER, depends_on_id INTEGER, relationship_type TEXT)")
        conn.commit()
        out = io.StringIO()
        env_no_pretty = {k: v for k, v in os.environ.items() if k != "TUSK_PRETTY"}
        with patch.dict(os.environ, env_no_pretty, clear=True), redirect_stdout(out):
            rc = deps_mod.show_blocked(conn, False)
        assert rc == 0
        assert json.loads(out.getvalue()) == []

    def test_pretty_env_renders_table(self):
        rc, stdout = _run(deps_mod.show_blocked, False, pretty_env="1")
        assert rc == 0
        assert "Blocked Tasks" in stdout

    def test_pretty_env_truthy_values(self):
        for value in ("1", "true", "yes", "on", "TRUE", "Yes"):
            rc, stdout = _run(deps_mod.show_blocked, False, pretty_env=value)
            assert rc == 0, f"rc != 0 for TUSK_PRETTY={value!r}"
            assert "Blocked Tasks" in stdout, (
                f"table not rendered for TUSK_PRETTY={value!r}"
            )

    def test_pretty_env_falsy_emits_json(self):
        for value in ("", "0", "false", "no"):
            rc, stdout = _run(deps_mod.show_blocked, False, pretty_env=value)
            assert rc == 0, f"rc != 0 for TUSK_PRETTY={value!r}"
            assert isinstance(json.loads(stdout), list)

    def test_explicit_json_flag_still_works(self):
        conn = _make_conn()
        out = io.StringIO()
        with patch.dict(os.environ, {"TUSK_PRETTY": "1"}, clear=False), redirect_stdout(out):
            rc = deps_mod.show_blocked(conn, True)
        assert rc == 0
        assert isinstance(json.loads(out.getvalue()), list)


# ── deps ready ───────────────────────────────────────────────────────


class TestDepsReadyJson:
    def test_default_emits_parseable_json_array(self):
        rc, stdout = _run(deps_mod.show_ready, False)
        assert rc == 0
        data = json.loads(stdout)
        assert isinstance(data, list)
        # task 4 (standalone, no deps) is ready
        assert any(t["id"] == 4 for t in data)
        first = next(t for t in data if t["id"] == 4)
        for key in ("id", "summary", "status", "priority", "dep_count"):
            assert key in first
        assert first["dep_count"] == 0

    def test_json_default_is_compact(self):
        rc, stdout = _run(deps_mod.show_ready, False)
        assert rc == 0
        assert "\n" not in stdout.strip()
        assert ", " not in stdout
        assert ": " not in stdout

    def test_empty_returns_empty_array(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, summary TEXT, status TEXT, priority TEXT)")
        conn.execute("CREATE TABLE task_dependencies (task_id INTEGER, depends_on_id INTEGER, relationship_type TEXT)")
        conn.execute("CREATE VIEW v_ready_tasks AS SELECT id, summary, status, priority FROM tasks WHERE 0")
        conn.commit()
        out = io.StringIO()
        env_no_pretty = {k: v for k, v in os.environ.items() if k != "TUSK_PRETTY"}
        with patch.dict(os.environ, env_no_pretty, clear=True), redirect_stdout(out):
            rc = deps_mod.show_ready(conn, False)
        assert rc == 0
        assert json.loads(out.getvalue()) == []

    def test_pretty_env_renders_table(self):
        rc, stdout = _run(deps_mod.show_ready, False, pretty_env="1")
        assert rc == 0
        assert "Ready Tasks" in stdout

    def test_pretty_env_truthy_values(self):
        for value in ("1", "true", "yes", "on", "TRUE", "Yes"):
            rc, stdout = _run(deps_mod.show_ready, False, pretty_env=value)
            assert rc == 0, f"rc != 0 for TUSK_PRETTY={value!r}"
            assert "Ready Tasks" in stdout, (
                f"table not rendered for TUSK_PRETTY={value!r}"
            )

    def test_pretty_env_falsy_emits_json(self):
        for value in ("", "0", "false", "no"):
            rc, stdout = _run(deps_mod.show_ready, False, pretty_env=value)
            assert rc == 0, f"rc != 0 for TUSK_PRETTY={value!r}"
            assert isinstance(json.loads(stdout), list)

    def test_explicit_json_flag_still_works(self):
        conn = _make_conn()
        out = io.StringIO()
        with patch.dict(os.environ, {"TUSK_PRETTY": "1"}, clear=False), redirect_stdout(out):
            rc = deps_mod.show_ready(conn, True)
        assert rc == 0
        assert isinstance(json.loads(out.getvalue()), list)


# ── deps all ─────────────────────────────────────────────────────────


class TestDepsAllJson:
    def test_default_emits_parseable_json_array(self):
        rc, stdout = _run(deps_mod.show_all, False)
        assert rc == 0
        data = json.loads(stdout)
        assert isinstance(data, list)
        assert len(data) == 2  # two task_dependencies rows
        first = data[0]
        for key in (
            "task_id", "task_summary", "task_status",
            "depends_on_id", "dep_summary", "dep_status",
            "relationship_type",
        ):
            assert key in first

    def test_json_default_is_compact(self):
        rc, stdout = _run(deps_mod.show_all, False)
        assert rc == 0
        assert "\n" not in stdout.strip()
        assert ", " not in stdout
        assert ": " not in stdout

    def test_empty_returns_empty_array(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, summary TEXT, status TEXT, priority TEXT)")
        conn.execute("CREATE TABLE task_dependencies (task_id INTEGER, depends_on_id INTEGER, relationship_type TEXT)")
        conn.commit()
        out = io.StringIO()
        env_no_pretty = {k: v for k, v in os.environ.items() if k != "TUSK_PRETTY"}
        with patch.dict(os.environ, env_no_pretty, clear=True), redirect_stdout(out):
            rc = deps_mod.show_all(conn, False)
        assert rc == 0
        assert json.loads(out.getvalue()) == []

    def test_pretty_env_renders_table(self):
        rc, stdout = _run(deps_mod.show_all, False, pretty_env="1")
        assert rc == 0
        assert "All Task Dependencies" in stdout

    def test_pretty_env_truthy_values(self):
        for value in ("1", "true", "yes", "on", "TRUE", "Yes"):
            rc, stdout = _run(deps_mod.show_all, False, pretty_env=value)
            assert rc == 0, f"rc != 0 for TUSK_PRETTY={value!r}"
            assert "All Task Dependencies" in stdout, (
                f"table not rendered for TUSK_PRETTY={value!r}"
            )

    def test_pretty_env_falsy_emits_json(self):
        for value in ("", "0", "false", "no"):
            rc, stdout = _run(deps_mod.show_all, False, pretty_env=value)
            assert rc == 0, f"rc != 0 for TUSK_PRETTY={value!r}"
            assert isinstance(json.loads(stdout), list)

    def test_explicit_json_flag_still_works(self):
        conn = _make_conn()
        out = io.StringIO()
        with patch.dict(os.environ, {"TUSK_PRETTY": "1"}, clear=False), redirect_stdout(out):
            rc = deps_mod.show_all(conn, True)
        assert rc == 0
        assert isinstance(json.loads(out.getvalue()), list)
