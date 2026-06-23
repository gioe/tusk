"""Unit tests for tusk-propose-work.py.

Covers the six acceptance-criterion node IDs:
- ranked_json       — output is a JSON array sorted highest-score first
- skill_patch       — unconfirmed skill-patch findings appear as a source
- next_steps        — unconsumed next_steps on open tasks appear as a source
- jots              — recurring jot categories appear as a source
- todo_scan         — a repo TODO/FIXME scan appears as a source
- empty_returns_array — an empty-signal environment returns [] (exit 0), not error

The fixture schema is a minimal subset of bin/tusk's real schema — only the
columns the aggregation queries read. No schema-sync guard targets these
tables, so this fixture intentionally does not mirror the canonical CREATE TABLE.
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_propose_work",
    os.path.join(BIN, "tusk-propose-work.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


_SCHEMA = """
CREATE TABLE skill_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    started_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT,
    status TEXT DEFAULT 'To Do',
    complexity TEXT
);
CREATE TABLE task_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    next_steps TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE retro_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_run_id INTEGER NOT NULL,
    task_id INTEGER,
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    action_taken TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE jots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_run_id INTEGER NOT NULL,
    task_id INTEGER,
    category TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE task_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    started_at TEXT
);
CREATE TABLE tool_call_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    tool_name TEXT NOT NULL,
    call_count INTEGER NOT NULL DEFAULT 0,
    total_cost REAL NOT NULL DEFAULT 0.0
);
"""


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO skill_runs (id, skill_name) VALUES (1, 'retro')")
    conn.commit()
    return conn


def _add_skill_patch(conn, target_file, *, confirmed=False):
    conn.execute(
        "INSERT INTO retro_findings (skill_run_id, task_id, category, summary, action_taken) "
        "VALUES (1, NULL, 'process', 'patch', ?)",
        (f"skill-patch:{target_file}",),
    )
    if confirmed:
        conn.execute(
            "INSERT INTO retro_findings (skill_run_id, task_id, category, summary, action_taken, created_at) "
            "VALUES (1, NULL, 'process', 'confirm', ?, datetime('now', '+1 day'))",
            (f"skill-patch-confirmed:{target_file}",),
        )
    conn.commit()


def _add_task(conn, task_id, summary, *, status="To Do", complexity=None):
    conn.execute(
        "INSERT INTO tasks (id, summary, status, complexity) VALUES (?, ?, ?, ?)",
        (task_id, summary, status, complexity),
    )
    conn.commit()


def _add_next_steps(conn, task_id, next_steps):
    conn.execute(
        "INSERT INTO task_progress (task_id, next_steps) VALUES (?, ?)",
        (task_id, next_steps),
    )
    conn.commit()


def _add_jot(conn, category, note):
    conn.execute(
        "INSERT INTO jots (skill_run_id, category, note) VALUES (1, ?, ?)",
        (category, note),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# ranked_json
# ---------------------------------------------------------------------------

def test_ranked_json_is_sorted_descending(tmp_path):
    conn = _make_conn()
    # Two sources of different base scores: a skill_patch (80+) outranks a TODO (30).
    _add_skill_patch(conn, "skills/foo/SKILL.md")
    repo = tmp_path / "repo"
    (repo).mkdir()
    (repo / "a.py").write_text("# TODO: add retry logic to the network client\n")

    proposals = mod.build_proposals(conn, str(repo))
    assert isinstance(proposals, list)
    assert len(proposals) >= 2
    scores = [p["score"] for p in proposals]
    assert scores == sorted(scores, reverse=True), "must be ranked highest-first"
    # Every proposal carries a source label and a numeric score.
    for p in proposals:
        assert isinstance(p["source"], str) and p["source"]
        assert isinstance(p["score"], (int, float))
    # The strongest source (skill_patch) is first.
    assert proposals[0]["source"] == "skill_patch"


def test_ranked_json_cli_emits_single_line_array(tmp_path):
    """The CLI entrypoint emits a single-line JSON array and exits 0."""
    db_path = str(tmp_path / "tusk" / "tasks.db")
    os.makedirs(os.path.dirname(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO skill_runs (id, skill_name) VALUES (1, 'retro')")
    conn.commit()
    conn.execute(
        "INSERT INTO retro_findings (skill_run_id, category, summary, action_taken) "
        "VALUES (1, 'process', 'p', 'skill-patch:skills/x/SKILL.md')"
    )
    conn.commit()
    conn.close()

    result = subprocess.run(
        [sys.executable, os.path.join(BIN, "tusk-propose-work.py"), db_path,
         "/dev/null", "--no-todo-scan"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip()
    assert "\n" not in out, "output must be a single line"
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert any(p["source"] == "skill_patch" for p in parsed)


# ---------------------------------------------------------------------------
# skill_patch
# ---------------------------------------------------------------------------

def test_skill_patch_source_included(tmp_path):
    conn = _make_conn()
    _add_skill_patch(conn, "skills/foo/SKILL.md")
    proposals = mod.build_proposals(conn, None, include_todo_scan=False)
    sp = [p for p in proposals if p["source"] == "skill_patch"]
    assert len(sp) == 1
    assert sp[0]["evidence"]["target_file"] == "skills/foo/SKILL.md"
    assert sp[0]["score"] > 0


def test_skill_patch_confirmed_excluded(tmp_path):
    conn = _make_conn()
    _add_skill_patch(conn, "skills/confirmed/SKILL.md", confirmed=True)
    _add_skill_patch(conn, "skills/open/SKILL.md")
    proposals = mod.build_proposals(conn, None, include_todo_scan=False)
    targets = {p["evidence"]["target_file"] for p in proposals if p["source"] == "skill_patch"}
    assert targets == {"skills/open/SKILL.md"}


# ---------------------------------------------------------------------------
# next_steps
# ---------------------------------------------------------------------------

def test_next_steps_source_included(tmp_path):
    conn = _make_conn()
    _add_task(conn, 10, "Open task", status="To Do")
    _add_next_steps(conn, 10, "Wire up the remaining edge case for nulls")
    proposals = mod.build_proposals(conn, None, include_todo_scan=False)
    ns = [p for p in proposals if p["source"] == "next_steps"]
    assert len(ns) == 1
    assert ns[0]["evidence"]["task_id"] == 10
    assert "edge case" in ns[0]["detail"]


def test_next_steps_done_task_excluded(tmp_path):
    conn = _make_conn()
    _add_task(conn, 11, "Closed task", status="Done")
    _add_next_steps(conn, 11, "This thread is already finished")
    proposals = mod.build_proposals(conn, None, include_todo_scan=False)
    ns = [p for p in proposals if p["source"] == "next_steps"]
    assert ns == []


# ---------------------------------------------------------------------------
# jots
# ---------------------------------------------------------------------------

def test_jots_recurring_category_included(tmp_path):
    conn = _make_conn()
    _add_jot(conn, "flaky-test", "test X flaked again")
    _add_jot(conn, "flaky-test", "test X flaked once more")
    _add_jot(conn, "one-off", "single note")  # below recurrence floor
    proposals = mod.build_proposals(conn, None, include_todo_scan=False)
    jot_props = [p for p in proposals if p["source"] == "jot_category"]
    cats = {p["evidence"]["category"] for p in jot_props}
    assert "flaky-test" in cats
    assert "one-off" not in cats, "single-occurrence categories are not recurring"
    flaky = next(p for p in jot_props if p["evidence"]["category"] == "flaky-test")
    assert flaky["evidence"]["count"] == 2


# ---------------------------------------------------------------------------
# todo_scan
# ---------------------------------------------------------------------------

def test_todo_scan_source_included(tmp_path):
    conn = _make_conn()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text(
        "def f():\n    # FIXME: handle the timeout case before shipping this\n    pass\n"
    )
    proposals = mod.build_proposals(conn, str(repo))
    todo_props = [p for p in proposals if p["source"] == "todo_scan"]
    assert len(todo_props) >= 1
    fixme = todo_props[0]
    assert fixme["evidence"]["keyword"] == "FIXME"
    assert fixme["evidence"]["file"] == "mod.py"


def test_todo_scan_skipped_when_disabled(tmp_path):
    conn = _make_conn()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("# TODO: this should not appear when disabled\n")
    proposals = mod.build_proposals(conn, str(repo), include_todo_scan=False)
    assert [p for p in proposals if p["source"] == "todo_scan"] == []


# ---------------------------------------------------------------------------
# empty_returns_array
# ---------------------------------------------------------------------------

def test_empty_returns_array(tmp_path):
    """An environment with no signals returns [] (not an error)."""
    conn = _make_conn()
    proposals = mod.build_proposals(conn, None)
    assert proposals == []


def test_empty_returns_array_cli_exit_zero(tmp_path):
    """The CLI entrypoint exits 0 and prints [] for an empty-signal DB."""
    db_path = str(tmp_path / "tusk" / "tasks.db")
    os.makedirs(os.path.dirname(db_path))
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()

    result = subprocess.run(
        [sys.executable, os.path.join(BIN, "tusk-propose-work.py"), db_path, "/dev/null"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == []
