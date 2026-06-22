"""Unit tests for tusk-objective.py — the objective CRUD + linking CLI."""

import json
import os
import sqlite3
import subprocess
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


# Minimal subset of the live schema: just the tables tusk-objective.py touches.
# objectives/objective_tasks mirror bin/tusk verbatim; tasks declares only the
# columns the CLI reads (id, summary, status).
_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'To Do'
);
CREATE TABLE objectives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed', 'abandoned')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at TEXT
);
CREATE TABLE objective_tasks (
    objective_id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    relationship_type TEXT NOT NULL DEFAULT 'contributes_to' CHECK (relationship_type IN ('primary', 'contributes_to', 'follow_up')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (objective_id, task_id),
    FOREIGN KEY (objective_id) REFERENCES objectives(id) ON DELETE CASCADE,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
"""


def _make_db(tmp_path):
    db_path = str(tmp_path / "objective.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO tasks (id, summary, status) VALUES (42, 'a task', 'To Do')")
    conn.execute("INSERT INTO tasks (id, summary, status) VALUES (43, 'another task', 'In Progress')")
    conn.commit()
    return db_path, conn


def _run_cli(db_path, *cli_args, config_path="fake.json"):
    return subprocess.run(
        [sys.executable, os.path.join(BIN, "tusk-objective.py"),
         db_path, config_path, *cli_args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _insert(db_path, summary, *extra):
    result = _run_cli(db_path, "insert", summary, *extra)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Criterion 3284 — each subcommand round-trips against a temp DB
# ---------------------------------------------------------------------------

def test_subcommands_round_trip(tmp_path):
    db_path, conn = _make_db(tmp_path)

    # insert
    obj = _insert(db_path, "Ship the objective layer", "--description", "the why")
    assert obj["status"] == "active"
    assert obj["summary"] == "Ship the objective layer"
    assert obj["description"] == "the why"
    oid = obj["id"]

    # list (default status=active) shows it with a task_count rollup
    result = _run_cli(db_path, "list")
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert [r["id"] for r in rows] == [oid]
    assert rows[0]["task_count"] == 0

    # link a task, then get reflects it
    result = _run_cli(db_path, "link", str(oid), "42", "--type", "primary")
    assert result.returncode == 0, result.stderr

    result = _run_cli(db_path, "get", str(oid))
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)
    assert got["id"] == oid
    assert len(got["tasks"]) == 1
    assert got["tasks"][0]["id"] == 42
    assert got["tasks"][0]["relationship_type"] == "primary"

    # update summary/description/status
    result = _run_cli(db_path, "update", str(oid), "--summary", "Renamed", "--status", "completed")
    assert result.returncode == 0, result.stderr
    updated = json.loads(result.stdout)
    assert updated["summary"] == "Renamed"
    assert updated["status"] == "completed"
    assert updated["closed_at"] is not None

    # update back to active clears closed_at
    result = _run_cli(db_path, "update", str(oid), "--status", "active")
    assert json.loads(result.stdout)["closed_at"] is None

    # unlink removes the link
    result = _run_cli(db_path, "unlink", str(oid), "42")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["removed"] is True

    result = _run_cli(db_path, "get", str(oid))
    assert json.loads(result.stdout)["tasks"] == []

    # OBJ- / TASK- prefix forms are accepted
    result = _run_cli(db_path, "get", f"OBJ-{oid}")
    assert result.returncode == 0, result.stderr


def test_list_status_filter_and_all(tmp_path):
    db_path, _ = _make_db(tmp_path)
    a = _insert(db_path, "active one")["id"]
    b = _insert(db_path, "to be closed")["id"]
    _run_cli(db_path, "done", str(b), "--reason", "completed")

    # default active filter hides the completed one
    rows = json.loads(_run_cli(db_path, "list").stdout)
    assert [r["id"] for r in rows] == [a]

    # --status all shows both
    rows = json.loads(_run_cli(db_path, "list", "--status", "all").stdout)
    assert {r["id"] for r in rows} == {a, b}

    # --status completed shows only the closed one
    rows = json.loads(_run_cli(db_path, "list", "--status", "completed").stdout)
    assert [r["id"] for r in rows] == [b]


def test_link_relationship_type_upsert(tmp_path):
    db_path, _ = _make_db(tmp_path)
    oid = _insert(db_path, "obj")["id"]
    _run_cli(db_path, "link", str(oid), "42", "--type", "contributes_to")
    # Re-linking the same pair updates the relationship type rather than erroring.
    result = _run_cli(db_path, "link", str(oid), "42", "--type", "primary")
    assert result.returncode == 0, result.stderr
    got = json.loads(_run_cli(db_path, "get", str(oid)).stdout)
    assert len(got["tasks"]) == 1
    assert got["tasks"][0]["relationship_type"] == "primary"


# ---------------------------------------------------------------------------
# Criterion 3285 — link/unlink enforce FK existence of objective and task
# ---------------------------------------------------------------------------

def test_link_unlink_enforce_fk(tmp_path):
    db_path, _ = _make_db(tmp_path)
    oid = _insert(db_path, "obj")["id"]

    # link against a missing objective
    result = _run_cli(db_path, "link", "9999", "42")
    assert result.returncode == 1
    assert "objective 9999 not found" in result.stderr

    # link against a missing task
    result = _run_cli(db_path, "link", str(oid), "9999")
    assert result.returncode == 1
    assert "task 9999 not found" in result.stderr

    # unlink against a missing objective
    result = _run_cli(db_path, "unlink", "9999", "42")
    assert result.returncode == 1
    assert "objective 9999 not found" in result.stderr

    # unlink a pair that was never linked succeeds with removed=False
    result = _run_cli(db_path, "unlink", str(oid), "42")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["removed"] is False


def test_get_and_done_missing_objective(tmp_path):
    db_path, _ = _make_db(tmp_path)
    assert _run_cli(db_path, "get", "9999").returncode == 1
    assert _run_cli(db_path, "done", "9999", "--reason", "completed").returncode == 1
    assert _run_cli(db_path, "update", "9999", "--summary", "x").returncode == 1


# ---------------------------------------------------------------------------
# Criterion 3286 — done leaves linked task status untouched
# ---------------------------------------------------------------------------

def test_done_leaves_tasks_untouched(tmp_path):
    db_path, conn = _make_db(tmp_path)
    oid = _insert(db_path, "obj")["id"]
    _run_cli(db_path, "link", str(oid), "42", "--type", "primary")
    _run_cli(db_path, "link", str(oid), "43", "--type", "contributes_to")

    result = _run_cli(db_path, "done", str(oid), "--reason", "abandoned")
    assert result.returncode == 0, result.stderr
    closed = json.loads(result.stdout)
    assert closed["status"] == "abandoned"
    assert closed["closed_at"] is not None

    # Linked tasks keep their own status and the links themselves survive.
    statuses = dict(conn.execute("SELECT id, status FROM tasks").fetchall())
    assert statuses[42] == "To Do"
    assert statuses[43] == "In Progress"
    link_count = conn.execute(
        "SELECT COUNT(*) FROM objective_tasks WHERE objective_id = ?", (oid,)
    ).fetchone()[0]
    assert link_count == 2


# ---------------------------------------------------------------------------
# Criterion 3287 — metachar guard rejects shell-substitution in summary/description
# ---------------------------------------------------------------------------

def test_metachar_guard_rejects_substitution(tmp_path):
    db_path, _ = _make_db(tmp_path)

    # backtick in summary
    result = _run_cli(db_path, "insert", "ship `whoami` now")
    assert result.returncode == 1
    assert "shell-substitution metacharacter" in result.stderr

    # $(...) in description
    result = _run_cli(db_path, "insert", "clean summary", "--description", "do $(rm -rf /)")
    assert result.returncode == 1
    assert "shell-substitution metacharacter" in result.stderr

    # ${...} in update summary
    oid = _insert(db_path, "clean")["id"]
    result = _run_cli(db_path, "update", str(oid), "--summary", "use ${HOME}")
    assert result.returncode == 1
    assert "shell-substitution metacharacter" in result.stderr

    # a clean insert still succeeds (guard does not over-reject)
    result = _run_cli(db_path, "insert", "a perfectly normal objective summary")
    assert result.returncode == 0, result.stderr
