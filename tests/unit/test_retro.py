"""Unit tests for tusk-retro.py (the orchestrator that bundles retro-signals
and retro-themes into one JSON blob).

The fixture combines minimal subsets of both consumed schemas — neither is
meant to mirror the canonical CREATE TABLE in bin/tusk, since no schema-sync
guard targets these tables (mirroring the convention from test_retro_signals
and test_retro_themes).
"""

import json
import os
import subprocess
import sqlite3
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


# Union of tables queried by build_signals (retro-signals) and fetch_themes
# (retro-themes). Columns trimmed to only what those modules SELECT.
_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    summary TEXT,
    description TEXT,
    status TEXT DEFAULT 'To Do',
    complexity TEXT,
    fixes_task_id INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE task_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    started_at TEXT,
    ended_at TEXT
);
CREATE TABLE task_status_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    changed_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE task_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    commit_hash TEXT,
    commit_message TEXT,
    files_changed TEXT,
    next_steps TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE acceptance_criteria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    criterion TEXT,
    is_completed INTEGER DEFAULT 0,
    is_deferred INTEGER DEFAULT 0,
    deferred_reason TEXT,
    skip_note TEXT
);
CREATE TABLE code_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    status TEXT DEFAULT 'pending'
);
CREATE TABLE review_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL,
    file_path TEXT,
    category TEXT,
    severity TEXT,
    comment TEXT NOT NULL,
    resolution TEXT
);
CREATE TABLE tool_call_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    tool_name TEXT NOT NULL,
    call_count INTEGER NOT NULL DEFAULT 0,
    total_cost REAL NOT NULL DEFAULT 0.0
);
CREATE TABLE skill_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    started_at TEXT DEFAULT (datetime('now'))
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
"""


def _make_db(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return db_path, conn


def _run_cli(db_path, *cli_args, config_path="fake.json"):
    result = subprocess.run(
        [sys.executable, os.path.join(BIN, "tusk-retro.py"), db_path, config_path, *cli_args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode, result.stdout, result.stderr


class TestCombinedShape:
    def test_done_task_with_rework_chain_returns_combined_blob(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        # Subject task: completed, with a follow-up that "fixes" it.
        conn.execute(
            "INSERT INTO tasks (id, summary, status, complexity) VALUES (1, 'subject', 'Done', 'M')"
        )
        conn.execute(
            "INSERT INTO tasks (id, summary, status, fixes_task_id) "
            "VALUES (2, 'follow-up', 'To Do', 1)"
        )
        # Unconsumed handoff note attached to the subject task.
        conn.execute(
            "INSERT INTO task_progress (task_id, next_steps) VALUES (1, 'finish the rest')"
        )
        # Cross-retro findings: 2 in category 'A', 1 in category 'B'.
        # min_recurrence=2 should keep only 'A'.
        conn.execute("INSERT INTO skill_runs (id, skill_name) VALUES (1, 'retro')")
        conn.executemany(
            "INSERT INTO retro_findings (skill_run_id, category, summary) VALUES (1, ?, 's')",
            [("A",), ("A",), ("B",)],
        )
        conn.commit()
        conn.close()

        rc, stdout, stderr = _run_cli(db_path, "1", "--min-recurrence", "2")
        assert rc == 0, stderr
        data = json.loads(stdout)

        assert set(data.keys()) == {"task_id", "signals", "themes"}
        assert data["task_id"] == 1

        signals = data["signals"]
        # build_signals always populates the same set of keys, even when empty.
        assert set(signals.keys()) == {
            "task_id", "complexity", "reopen_count", "rework_chain",
            "review_themes", "skipped_criteria",
            "tool_call_outliers", "tool_errors", "unconsumed_next_steps",
        }
        assert signals["task_id"] == 1
        assert signals["complexity"] == "M"

        # Rework chain: task 2 fixes task 1 → fixed_by direction.
        assert signals["rework_chain"]["fixes"] == []
        assert signals["rework_chain"]["fixed_by"] == [
            {"id": 2, "summary": "follow-up", "status": "To Do"}
        ]

        # Unconsumed next-steps: the one row we seeded.
        assert len(signals["unconsumed_next_steps"]) == 1
        assert signals["unconsumed_next_steps"][0]["next_steps"] == "finish the rest"

        themes = data["themes"]
        assert set(themes.keys()) == {"window_days", "min_recurrence", "total_findings", "themes"}
        assert themes["min_recurrence"] == 2
        assert themes["total_findings"] == 3  # pre-HAVING count
        assert themes["themes"] == [{"theme": "A", "count": 2}]

    def test_task_prefix_form_resolves(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        conn.execute("INSERT INTO tasks (id, summary, status) VALUES (42, 's', 'Done')")
        conn.commit()
        conn.close()

        rc, stdout, stderr = _run_cli(db_path, "TASK-42")
        assert rc == 0, stderr
        assert json.loads(stdout)["task_id"] == 42


class TestErrorPaths:
    def test_missing_task_returns_nonzero(self, tmp_path):
        db_path, _ = _make_db(tmp_path)
        rc, _, stderr = _run_cli(db_path, "999")
        assert rc == 1
        assert "not found" in stderr.lower()

    def test_invalid_task_id_returns_nonzero(self, tmp_path):
        db_path, _ = _make_db(tmp_path)
        rc, _, stderr = _run_cli(db_path, "not-a-number")
        assert rc == 1
        assert "invalid task id" in stderr.lower()

    def test_negative_window_days_returns_nonzero(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        conn.execute("INSERT INTO tasks (id, summary, status) VALUES (1, 's', 'Done')")
        conn.commit()
        conn.close()
        rc, _, stderr = _run_cli(db_path, "1", "--window-days", "-1")
        assert rc == 1
        assert "window-days" in stderr

    def test_zero_min_recurrence_returns_nonzero(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        conn.execute("INSERT INTO tasks (id, summary, status) VALUES (1, 's', 'Done')")
        conn.commit()
        conn.close()
        rc, _, stderr = _run_cli(db_path, "1", "--min-recurrence", "0")
        assert rc == 1
        assert "min-recurrence" in stderr
