"""Unit tests for objective navigation from the task side (TASK-704).

Covers the three task-side surfaces that make objectives discoverable:
- `tusk task-list --objective <id>` returns only tasks linked to that objective
- `tusk task-get <id>` output includes a linked ``objectives`` array
- the canonical glossary ships an ``objective`` term that `glossary get` resolves
"""

import json
import os
import sqlite3
import subprocess
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


# Minimal subset of the live schema. `tasks` declares the columns task-list
# SELECTs and filters on (plus enough for task-get's SELECT *); objectives /
# objective_tasks mirror bin/tusk verbatim; acceptance_criteria / task_progress /
# glossary declare only what the queries under test read.
_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'To Do',
    priority TEXT,
    priority_score INTEGER DEFAULT 0,
    domain TEXT,
    assignee TEXT,
    complexity TEXT,
    task_type TEXT,
    workflow TEXT,
    not_before TEXT,
    bakeoff_id INTEGER,
    bakeoff_shadow INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
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
CREATE TABLE acceptance_criteria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    criterion TEXT NOT NULL,
    source TEXT,
    is_completed INTEGER NOT NULL DEFAULT 0,
    criterion_type TEXT,
    verification_spec TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE task_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    note TEXT,
    next_steps TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE glossary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL UNIQUE,
    definition TEXT NOT NULL,
    see_also TEXT,
    topics TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


def _make_db(tmp_path):
    db_path = str(tmp_path / "navigation.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    return db_path, conn


def _config(tmp_path):
    """A minimal real config file — tusk-glossary.py's main() loads it eagerly."""
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{}")
    return path


def _run(script, db_path, *cli_args, config_path="fake.json"):
    return subprocess.run(
        [sys.executable, os.path.join(BIN, script), db_path, config_path, *cli_args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Criterion 3295 — task-list --objective returns only linked tasks
# ---------------------------------------------------------------------------

def test_task_list_objective_filter(tmp_path):
    db_path, conn = _make_db(tmp_path)
    conn.executemany(
        "INSERT INTO tasks (id, summary, status) VALUES (?, ?, 'To Do')",
        [(1, "linked one"), (2, "linked two"), (3, "unrelated")],
    )
    conn.execute("INSERT INTO objectives (id, summary) VALUES (1, 'obj one')")
    conn.execute("INSERT INTO objectives (id, summary) VALUES (2, 'obj two')")
    conn.executemany(
        "INSERT INTO objective_tasks (objective_id, task_id) VALUES (?, ?)",
        [(1, 1), (1, 2), (2, 3)],
    )
    conn.commit()

    # The filter returns only tasks linked to the named objective.
    result = _run("tusk-task-list.py", db_path, "--objective", "1", "--format", "json")
    assert result.returncode == 0, result.stderr
    assert {row["id"] for row in json.loads(result.stdout)} == {1, 2}

    # A different objective returns only its own task.
    result = _run("tusk-task-list.py", db_path, "--objective", "2", "--format", "json")
    assert {row["id"] for row in json.loads(result.stdout)} == {3}

    # Without the filter, every task is listed (the filter is purely additive).
    result = _run("tusk-task-list.py", db_path, "--format", "json")
    assert {row["id"] for row in json.loads(result.stdout)} == {1, 2, 3}

    # An objective with no links yields an empty result, not an error.
    result = _run("tusk-task-list.py", db_path, "--objective", "999", "--format", "json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


# ---------------------------------------------------------------------------
# Criterion 3297 — task-get output includes linked objectives
# ---------------------------------------------------------------------------

def test_task_get_shows_objectives(tmp_path):
    db_path, conn = _make_db(tmp_path)
    conn.execute("INSERT INTO tasks (id, summary, status) VALUES (5, 'a task', 'In Progress')")
    conn.execute("INSERT INTO tasks (id, summary, status) VALUES (6, 'lonely', 'To Do')")
    conn.execute("INSERT INTO objectives (id, summary, status) VALUES (1, 'ship the layer', 'active')")
    conn.execute("INSERT INTO objectives (id, summary, status) VALUES (2, 'second initiative', 'active')")
    conn.execute(
        "INSERT INTO objective_tasks (objective_id, task_id, relationship_type) "
        "VALUES (1, 5, 'primary')"
    )
    conn.execute(
        "INSERT INTO objective_tasks (objective_id, task_id, relationship_type) "
        "VALUES (2, 5, 'contributes_to')"
    )
    conn.commit()

    result = _run("tusk-task-get.py", db_path, "5")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "objectives" in payload

    objs = payload["objectives"]
    # Ordered by relationship_type then objective id: 'contributes_to' (2) before 'primary' (1).
    assert [o["objective_id"] for o in objs] == [2, 1]
    by_id = {o["objective_id"]: o for o in objs}
    assert by_id[1]["relationship_type"] == "primary"
    assert by_id[1]["summary"] == "ship the layer"
    assert by_id[1]["status"] == "active"
    assert by_id[2]["relationship_type"] == "contributes_to"

    # A task with no objective links gets an empty array, not a missing key.
    result = _run("tusk-task-get.py", db_path, "6")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["objectives"] == []


# ---------------------------------------------------------------------------
# Criterion 3296 — glossary ships an `objective` term that `get` resolves
# ---------------------------------------------------------------------------

def test_glossary_objective_term(tmp_path):
    db_path, _ = _make_db(tmp_path)
    config_path = _config(tmp_path)
    glossary_md = os.path.join(REPO_ROOT, "docs", "GLOSSARY.md")

    # Seed the temp glossary from the canonical markdown (mirrors migration 75's
    # sync-from-md seed on a fresh install).
    result = _run(
        "tusk-glossary.py", db_path, "sync-from-md", "--file", glossary_md,
        config_path=config_path,
    )
    assert result.returncode == 0, result.stderr

    # `glossary get objective` resolves to a non-empty definition.
    result = _run("tusk-glossary.py", db_path, "get", "objective", config_path=config_path)
    assert result.returncode == 0, result.stderr
    entry = json.loads(result.stdout)
    assert entry["term"] == "objective"
    assert entry["definition"].strip()
    # The definition ties objectives to their constituent tasks.
    assert "task" in entry["definition"].lower()
