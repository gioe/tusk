"""Unit tests for `tusk objective brief` — the read-side rollup over linked tasks.

The brief aggregates status breakdown, criteria coverage, summed cost/duration,
and open objective-scoped context items across an objective's linked tasks. The
critical invariant (the task's active risk) is that cost/criteria are summed per
DISTINCT task: a task linked to multiple objectives, or a task with several
sessions and criteria, must never be double-counted via a multi-join fan-out.
"""

import json
import os
import sqlite3
import subprocess
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


# Minimal slice of the live schema needed by `objective brief`. The task_metrics
# and v_criteria_coverage views are copied verbatim from bin/tusk so the brief's
# aggregation runs against the real view shapes; tasks carries bakeoff_shadow
# because both views filter on it, and task_status_transitions exists because
# task_metrics' reopen_count subquery references it.
_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'To Do',
    bakeoff_shadow INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE task_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_seconds INTEGER,
    active_seconds INTEGER,
    cost_dollars REAL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    lines_added INTEGER,
    lines_removed INTEGER,
    request_count INTEGER,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE UNIQUE INDEX idx_task_sessions_open ON task_sessions(task_id) WHERE ended_at IS NULL;
CREATE TABLE task_status_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE TABLE acceptance_criteria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    criterion TEXT NOT NULL,
    is_completed INTEGER DEFAULT 0,
    is_deferred INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    CHECK (is_completed IN (0, 1)),
    CHECK (is_deferred IN (0, 1))
);
CREATE TABLE task_context_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    objective_id INTEGER,
    item_type TEXT NOT NULL CHECK (item_type IN ('memory', 'assumption', 'question', 'risk', 'decision', 'entry_point')),
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'resolved', 'superseded')),
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (objective_id) REFERENCES objectives(id) ON DELETE SET NULL
);
CREATE VIEW task_metrics AS
SELECT t.*,
    COUNT(s.id) as session_count,
    SUM(s.duration_seconds) as total_duration_seconds,
    SUM(s.cost_dollars) as total_cost,
    SUM(s.tokens_in) as total_tokens_in,
    SUM(s.tokens_out) as total_tokens_out,
    SUM(s.lines_added) as total_lines_added,
    SUM(s.lines_removed) as total_lines_removed,
    SUM(s.request_count) as total_request_count,
    (SELECT COUNT(*) FROM task_status_transitions tst
      WHERE tst.task_id = t.id AND tst.to_status = 'To Do') as reopen_count
FROM tasks t
LEFT JOIN task_sessions s ON t.id = s.task_id
WHERE t.bakeoff_shadow = 0
GROUP BY t.id;
CREATE VIEW v_criteria_coverage AS
SELECT t.id AS task_id,
       t.summary,
       COUNT(CASE WHEN ac.is_deferred = 0 OR ac.is_deferred IS NULL THEN 1 END) AS total_criteria,
       COALESCE(SUM(CASE WHEN ac.is_completed = 1 AND (ac.is_deferred = 0 OR ac.is_deferred IS NULL) THEN 1 ELSE 0 END), 0) AS completed_criteria,
       COUNT(CASE WHEN ac.is_deferred = 0 OR ac.is_deferred IS NULL THEN 1 END) - COALESCE(SUM(CASE WHEN ac.is_completed = 1 AND (ac.is_deferred = 0 OR ac.is_deferred IS NULL) THEN 1 ELSE 0 END), 0) AS remaining_criteria
FROM tasks t
LEFT JOIN acceptance_criteria ac ON ac.task_id = t.id
WHERE t.bakeoff_shadow = 0
GROUP BY t.id, t.summary;
"""


def _make_db(tmp_path):
    db_path = str(tmp_path / "objective_brief.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    return db_path, conn


def _run_brief(db_path, objective_id, fmt="json"):
    return subprocess.run(
        [sys.executable, os.path.join(BIN, "tusk-objective.py"),
         db_path, "fake.json", "brief", str(objective_id), "--format", fmt],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _add_task(conn, task_id, summary, status):
    conn.execute(
        "INSERT INTO tasks (id, summary, status) VALUES (?, ?, ?)",
        (task_id, summary, status),
    )


def _add_session(conn, task_id, cost, duration):
    conn.execute(
        "INSERT INTO task_sessions (task_id, started_at, ended_at, duration_seconds, cost_dollars) "
        "VALUES (?, '2026-01-01 00:00:00', '2026-01-01 01:00:00', ?, ?)",
        (task_id, duration, cost),
    )


def _add_criterion(conn, task_id, completed):
    conn.execute(
        "INSERT INTO acceptance_criteria (task_id, criterion, is_completed) VALUES (?, 'c', ?)",
        (task_id, 1 if completed else 0),
    )


def _link(conn, objective_id, task_id, rel="contributes_to"):
    conn.execute(
        "INSERT INTO objective_tasks (objective_id, task_id, relationship_type) VALUES (?, ?, ?)",
        (objective_id, task_id, rel),
    )


# ---------------------------------------------------------------------------
# Criterion 3288 — brief renders both JSON and markdown formats
# ---------------------------------------------------------------------------

def test_renders_json_and_markdown(tmp_path):
    db_path, conn = _make_db(tmp_path)
    conn.execute("INSERT INTO objectives (id, summary, description, status) VALUES (1, 'Ship the layer', 'the why', 'active')")
    _add_task(conn, 42, "first task", "To Do")
    _add_task(conn, 43, "second task", "In Progress")
    _link(conn, 1, 42, "primary")
    _link(conn, 1, 43, "contributes_to")
    _add_session(conn, 42, 1.5, 120)
    _add_session(conn, 43, 0.5, 60)
    _add_criterion(conn, 42, True)
    _add_criterion(conn, 42, False)
    _add_criterion(conn, 43, True)
    conn.execute(
        "INSERT INTO task_context_items (task_id, objective_id, item_type, content, status) "
        "VALUES (42, 1, 'risk', 'a scoped risk', 'active')"
    )
    conn.commit()

    # JSON shape
    result = _run_brief(db_path, 1, "json")
    assert result.returncode == 0, result.stderr
    brief = json.loads(result.stdout)
    assert brief["objective"]["id"] == 1
    assert brief["task_count"] == 2
    assert brief["status_breakdown"] == {"To Do": 1, "In Progress": 1}
    assert brief["criteria"] == {"total": 3, "completed": 2, "remaining": 1}
    assert brief["cost"]["total_cost_dollars"] == 2.0
    assert brief["cost"]["total_duration_seconds"] == 180
    assert brief["cost"]["session_count"] == 2
    assert len(brief["context_items"]) == 1
    assert brief["context_items"][0]["item_type"] == "risk"
    assert {t["id"] for t in brief["tasks"]} == {42, 43}

    # Markdown shape
    result = _run_brief(db_path, 1, "markdown")
    assert result.returncode == 0, result.stderr
    md = result.stdout
    assert "## OBJ-1 — Ship the layer (active)" in md
    assert "## Linked Tasks" in md
    assert "TASK-42" in md and "TASK-43" in md
    assert "## Open Context" in md
    assert "risk: a scoped risk" in md
    assert "$2.0000" in md
    # OBJ- prefix form is also accepted
    assert _run_brief(db_path, "OBJ-1", "json").returncode == 0


# ---------------------------------------------------------------------------
# Criterion 3289 — brief with no linked tasks renders gracefully
# ---------------------------------------------------------------------------

def test_empty_objective_renders(tmp_path):
    db_path, conn = _make_db(tmp_path)
    conn.execute("INSERT INTO objectives (id, summary, status) VALUES (2, 'Empty objective', 'active')")
    conn.commit()

    result = _run_brief(db_path, 2, "json")
    assert result.returncode == 0, result.stderr
    brief = json.loads(result.stdout)
    assert brief["task_count"] == 0
    assert brief["status_breakdown"] == {}
    assert brief["tasks"] == []
    assert brief["criteria"] == {"total": 0, "completed": 0, "remaining": 0}
    assert brief["cost"] == {"total_cost_dollars": 0, "total_duration_seconds": 0, "session_count": 0}
    assert brief["context_items"] == []

    # Markdown renders without error and shows the empty-list sentinel.
    result = _run_brief(db_path, 2, "markdown")
    assert result.returncode == 0, result.stderr
    md = result.stdout
    assert "## OBJ-2 — Empty objective (active)" in md
    assert "## Linked Tasks\n- None" in md
    assert "## Open Context\n- None" in md

    # A missing objective is a clean error, not a crash.
    missing = _run_brief(db_path, 999, "json")
    assert missing.returncode == 1
    assert "objective 999 not found" in missing.stderr


# ---------------------------------------------------------------------------
# Criterion 3290 — cost aggregation sums per distinct task, no double-count
# ---------------------------------------------------------------------------

def test_cost_no_double_count(tmp_path):
    db_path, conn = _make_db(tmp_path)
    conn.execute("INSERT INTO objectives (id, summary, status) VALUES (1, 'Briefed', 'active')")
    conn.execute("INSERT INTO objectives (id, summary, status) VALUES (2, 'Sibling', 'active')")
    _add_task(conn, 42, "shared task", "In Progress")
    # The same task is linked to BOTH objectives — its cost must count once in
    # objective 1's brief regardless of the sibling link.
    _link(conn, 1, 42, "primary")
    _link(conn, 2, 42, "contributes_to")
    # Two sessions ($1 + $2) and three criteria. A naive
    # objective_tasks⋈task_sessions⋈acceptance_criteria join would fan the cost
    # out to 2×3 = 6 rows and report $9.00; the per-distinct-task aggregation
    # must report exactly $3.00.
    _add_session(conn, 42, 1.0, 100)
    _add_session(conn, 42, 2.0, 200)
    _add_criterion(conn, 42, True)
    _add_criterion(conn, 42, True)
    _add_criterion(conn, 42, False)
    conn.commit()

    result = _run_brief(db_path, 1, "json")
    assert result.returncode == 0, result.stderr
    brief = json.loads(result.stdout)

    assert brief["task_count"] == 1
    assert brief["status_breakdown"] == {"In Progress": 1}
    assert brief["cost"]["total_cost_dollars"] == 3.0
    assert brief["cost"]["total_duration_seconds"] == 300
    assert brief["cost"]["session_count"] == 2
    assert brief["criteria"] == {"total": 3, "completed": 2, "remaining": 1}
