"""Unit tests for tusk-cost.py."""

import importlib.util
import json
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_cost",
    os.path.join(BIN, "tusk-cost.py"),
)
cost = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cost)


_SCHEMA = """
CREATE TABLE task_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    started_at TEXT,
    ended_at TEXT,
    cost_dollars REAL
);
CREATE TABLE skill_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    task_id INTEGER,
    started_at TEXT,
    ended_at TEXT,
    cost_dollars REAL
);
"""


def _make_db(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return db_path, conn


def test_cost_summary_dedupes_tusk_skill_run_shadows(tmp_path):
    db_path, conn = _make_db(tmp_path)
    conn.executemany(
        "INSERT INTO task_sessions (id, task_id, started_at, ended_at, cost_dollars) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (1, 101, "2026-05-01 10:00:00", "2026-05-01 10:30:00", 10.00),
            (2, 102, "2026-05-01 11:00:00", "2026-05-01 11:30:00", None),
            (3, 103, "2026-05-01 12:00:00", "2026-05-01 12:30:00", 5.00),
        ],
    )
    conn.executemany(
        "INSERT INTO skill_runs (id, skill_name, task_id, started_at, ended_at, cost_dollars) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "tusk", 101, "2026-05-01 10:00:05", "2026-05-01 10:30:00", 9.90),
            (2, "retro", 101, "2026-05-01 10:31:00", "2026-05-01 10:35:00", 1.25),
            (3, "review-commits", 101, "2026-05-01 10:35:00", "2026-05-01 10:40:00", None),
            (4, "tusk", 102, "2026-05-01 11:00:03", "2026-05-01 11:30:00", 2.00),
            (5, "tusk", 999, "2026-05-01 13:00:00", "2026-05-01 13:30:00", 3.00),
        ],
    )
    conn.commit()

    summary = cost.build_cost_summary(conn)

    assert summary["total_cost_dollars"] == 21.25
    assert summary["task_session_cost_dollars"] == 15.00
    assert summary["additional_skill_run_cost_dollars"] == 6.25
    assert summary["deduped_tusk_skill_run_cost_dollars"] == 9.90
    assert summary["task_sessions"]["costed"] == 2
    assert summary["task_sessions"]["missing_cost"] == 1
    assert summary["skill_runs"]["included_costed"] == 3
    assert summary["skill_runs"]["deduped_tusk_shadows"] == 1
    assert summary["skill_runs"]["missing_cost"] == 1


def test_main_emits_json_summary(tmp_path, capsys):
    db_path, conn = _make_db(tmp_path)
    conn.execute(
        "INSERT INTO task_sessions (task_id, started_at, cost_dollars) "
        "VALUES (1, '2026-05-01 10:00:00', 4.5)"
    )
    conn.commit()
    conn.close()

    rc = cost.main([db_path, "config.json", "--format", "json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_cost_dollars"] == 4.5
    assert payload["coverage"]["task_sessions_missing_cost"] == 0
