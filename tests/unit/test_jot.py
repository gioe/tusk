"""Unit tests for tusk-jot.py.

Covers (per TASK-331 criterion 1523):
- happy path: write with --file and --skill hints round-trips through the row
- write copies task_id from the active skill_run (not passed in by caller)
- write fails with exit 1 and a recovery hint when no skill_run is open
  (the most-recent row has ended_at != NULL — the active-run resolver must
  treat that as "no active run", not "use the most-recent row regardless")
- empty category / empty note → exit 1
- list filters by skill_run_id and task_id; both filters compose
- list with no filters returns the most-recent N globally
- ON DELETE CASCADE behaves: deleting the parent skill_run row removes
  the jot (the schema invariant that lets retro safely query by run-id
  without orphan worry)

The fixture schema is a minimal subset of bin/tusk's canonical CREATE TABLE —
jots + skill_runs + tasks. Mirrors test_retro_finding.py's convention.
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
    "tusk_jot",
    os.path.join(BIN, "tusk-jot.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


_SCHEMA = """
CREATE TABLE skill_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    started_at TEXT DEFAULT (datetime('now')),
    ended_at TEXT,
    task_id INTEGER
);
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT
);
CREATE TABLE task_workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    branch TEXT NOT NULL UNIQUE,
    workspace_path TEXT NOT NULL UNIQUE,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE TABLE jots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_run_id INTEGER NOT NULL,
    task_id INTEGER,
    category TEXT NOT NULL,
    note TEXT NOT NULL,
    file_hint TEXT,
    skill_hint TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (skill_run_id) REFERENCES skill_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);
"""


def _make_db(tmp_path, *, with_open_run=True):
    db_path = str(tmp_path / "jots.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # FK enforcement is off by default per-connection — turn it on so the
    # CASCADE test exercises the real production-shape behavior.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA + """
CREATE TABLE objectives (id INTEGER PRIMARY KEY);
CREATE TABLE task_context_items (
 id INTEGER PRIMARY KEY AUTOINCREMENT,task_id INTEGER NOT NULL,objective_id INTEGER,
 item_type TEXT NOT NULL,content TEXT NOT NULL,status TEXT DEFAULT 'active',source TEXT DEFAULT 'manual',
 created_at TEXT DEFAULT (datetime('now')),updated_at TEXT DEFAULT (datetime('now')),resolved_at TEXT
);
""")
    mod.tusk_loader.load("tusk-observation-lib").upgrade_schema(conn)
    conn.execute("INSERT INTO tasks (id, summary) VALUES (42, 'parent task')")
    if with_open_run:
        conn.execute(
            "INSERT INTO skill_runs (id, skill_name, task_id) VALUES (1, 'tusk', 42)"
        )
    else:
        # Closed run — the resolver must reject this.
        conn.execute(
            "INSERT INTO skill_runs (id, skill_name, task_id, ended_at) "
            "VALUES (1, 'tusk', 42, datetime('now'))"
        )
    conn.commit()
    return db_path, conn


def _run_cli(db_path, *cli_args, config_path="fake.json"):
    result = subprocess.run(
        [sys.executable, os.path.join(BIN, "tusk-jot.py"),
         db_path, config_path, *cli_args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode, result.stdout, result.stderr


def test_write_happy_path_with_hints(tmp_path):
    db_path, conn = _make_db(tmp_path)
    rc, out, err = _run_cli(
        db_path, "write", "process", "tusk skill-run start failed silently",
        "--file", "bin/tusk-skill-run.py",
        "--skill", "tusk",
    )
    assert rc == 0, err
    row = json.loads(out)
    assert row["category"] == "process"
    assert row["note"] == "tusk skill-run start failed silently"
    assert row["file_hint"] == "bin/tusk-skill-run.py"
    assert row["skill_hint"] == "tusk"
    # task_id was copied from the active skill_run, not passed in
    assert row["task_id"] == 42
    assert row["skill_run_id"] == 1


def test_write_no_hints_stores_real_nulls(tmp_path):
    """Omitting --file / --skill must produce SQL NULL, not the string 'None'."""
    db_path, conn = _make_db(tmp_path)
    rc, out, err = _run_cli(db_path, "write", "velocity", "spent 20m on venv mismatch")
    assert rc == 0, err
    row = json.loads(out)
    assert row["file_hint"] is None
    assert row["skill_hint"] is None


def _add_open_run(conn, *, run_id, task_id, started_at):
    conn.execute(
        "INSERT INTO tasks (id, summary) VALUES (?, ?)",
        (task_id, f"task {task_id}"),
    )
    conn.execute(
        "INSERT INTO skill_runs (id, skill_name, task_id, started_at) "
        "VALUES (?, 'tusk', ?, ?)",
        (run_id, task_id, started_at),
    )
    conn.commit()


def test_write_explicit_skill_run_targets_requested_parallel_run(tmp_path):
    db_path, conn = _make_db(tmp_path)
    _add_open_run(
        conn, run_id=2, task_id=43, started_at="2099-01-01 00:00:00"
    )

    rc, out, err = _run_cli(
        db_path, "write", "process", "belongs to run one", "--skill-run-id", "1"
    )

    assert rc == 0, err
    row = json.loads(out)
    assert row["skill_run_id"] == 1
    assert row["task_id"] == 42


def test_write_explicit_task_targets_its_unique_open_run(tmp_path):
    db_path, conn = _make_db(tmp_path)
    _add_open_run(
        conn, run_id=2, task_id=43, started_at="2099-01-01 00:00:00"
    )

    rc, out, err = _run_cli(
        db_path, "write", "process", "belongs to task 42", "--task-id", "42"
    )

    assert rc == 0, err
    row = json.loads(out)
    assert row["skill_run_id"] == 1
    assert row["task_id"] == 42


def test_write_explicit_run_and_task_must_agree(tmp_path):
    db_path, conn = _make_db(tmp_path)
    _add_open_run(
        conn, run_id=2, task_id=43, started_at="2099-01-01 00:00:00"
    )

    rc, out, err = _run_cli(
        db_path,
        "write",
        "process",
        "must not be stored",
        "--skill-run-id",
        "2",
        "--task-id",
        "42",
    )

    assert rc == 1
    assert out == ""
    assert "belongs to task 43, not requested task 42" in err
    assert conn.execute("SELECT COUNT(*) FROM jots").fetchone()[0] == 0


def test_write_explicit_run_must_be_open(tmp_path):
    db_path, conn = _make_db(tmp_path)
    conn.execute(
        "UPDATE skill_runs SET ended_at = '2099-01-01 00:00:00' WHERE id = 1"
    )
    conn.commit()

    rc, out, err = _run_cli(
        db_path, "write", "process", "must not be stored", "--skill-run-id", "1"
    )

    assert rc == 1
    assert out == ""
    assert "Skill run 1 is closed" in err
    assert conn.execute("SELECT COUNT(*) FROM jots").fetchone()[0] == 0


def test_write_caller_workspace_targets_its_task(tmp_path):
    db_path, conn = _make_db(tmp_path)
    caller_root = tmp_path / "task-42-worktree"
    caller_root.mkdir()
    conn.execute(
        "INSERT INTO task_workspaces (task_id, branch, workspace_path) "
        "VALUES (42, 'feature/TASK-42-test', ?)",
        (str(caller_root),),
    )
    _add_open_run(
        conn, run_id=2, task_id=43, started_at="2099-01-01 00:00:00"
    )

    rc, out, err = _run_cli(
        db_path,
        "--caller-root",
        str(caller_root),
        "write",
        "process",
        "belongs to task 42",
    )

    assert rc == 0, err
    row = json.loads(out)
    assert row["skill_run_id"] == 1
    assert row["task_id"] == 42


def test_write_unmapped_parallel_runs_refuses_ambiguity(tmp_path):
    db_path, conn = _make_db(tmp_path)
    _add_open_run(
        conn, run_id=2, task_id=43, started_at="2099-01-01 00:00:00"
    )

    rc, out, err = _run_cli(db_path, "write", "process", "must not be stored")

    assert rc == 1
    assert out == ""
    assert "Ambiguous jot target" in err
    assert "--skill-run-id <id> or --task-id <id>" in err
    assert conn.execute("SELECT COUNT(*) FROM jots").fetchone()[0] == 0


def test_write_task_with_multiple_open_runs_requires_exact_run(tmp_path):
    db_path, conn = _make_db(tmp_path)
    conn.execute(
        "INSERT INTO skill_runs (id, skill_name, task_id, started_at) "
        "VALUES (2, 'review-commits', 42, '2099-01-01 00:00:00')"
    )
    conn.commit()

    rc, out, err = _run_cli(
        db_path, "write", "process", "must not be stored", "--task-id", "42"
    )

    assert rc == 1
    assert out == ""
    assert "task 42 has multiple open skill runs" in err
    assert "--skill-run-id <id>" in err
    assert conn.execute("SELECT COUNT(*) FROM jots").fetchone()[0] == 0


def test_write_no_active_run_errors(tmp_path):
    """When the only skill_run row is already closed, write must exit 1."""
    db_path, conn = _make_db(tmp_path, with_open_run=False)
    rc, out, err = _run_cli(db_path, "write", "process", "anything")
    assert rc == 1
    assert "No active skill_run" in err
    assert "tusk skill-run start" in err  # recovery hint
    # No row was inserted
    assert conn.execute("SELECT COUNT(*) FROM jots").fetchone()[0] == 0


def test_write_empty_category_errors(tmp_path):
    db_path, _ = _make_db(tmp_path)
    rc, _, err = _run_cli(db_path, "write", "   ", "real note text")
    assert rc == 1
    assert "category" in err


def test_write_empty_note_errors(tmp_path):
    db_path, _ = _make_db(tmp_path)
    rc, _, err = _run_cli(db_path, "write", "process", "  ")
    assert rc == 1
    assert "note" in err


def test_list_filters_by_skill_run_id(tmp_path):
    db_path, conn = _make_db(tmp_path)
    # Seed two runs, one jot each
    conn.execute(
        "INSERT INTO skill_runs (id, skill_name, task_id, ended_at) "
        "VALUES (2, 'tusk', 42, datetime('now'))"
    )
    conn.execute("INSERT INTO jots (skill_run_id, task_id, category, note) "
                 "VALUES (1, 42, 'A', 'jot from run 1')")
    conn.execute("INSERT INTO jots (skill_run_id, task_id, category, note) "
                 "VALUES (2, 42, 'B', 'jot from run 2')")
    conn.commit()

    rc, out, err = _run_cli(db_path, "list", "--skill-run-id", "1")
    assert rc == 0, err
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0]["note"] == "jot from run 1"


def test_list_filters_by_task_id(tmp_path):
    db_path, conn = _make_db(tmp_path)
    conn.execute("INSERT INTO tasks (id, summary) VALUES (43, 'other task')")
    conn.execute(
        "INSERT INTO skill_runs (id, skill_name, task_id, ended_at) "
        "VALUES (2, 'tusk', 43, datetime('now'))"
    )
    conn.execute("INSERT INTO jots (skill_run_id, task_id, category, note) "
                 "VALUES (1, 42, 'A', 'jot for task 42')")
    conn.execute("INSERT INTO jots (skill_run_id, task_id, category, note) "
                 "VALUES (2, 43, 'B', 'jot for task 43')")
    conn.commit()

    rc, out, err = _run_cli(db_path, "list", "--task-id", "43")
    assert rc == 0, err
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0]["note"] == "jot for task 43"


def test_list_no_filters_returns_all(tmp_path):
    db_path, conn = _make_db(tmp_path)
    conn.execute("INSERT INTO jots (skill_run_id, task_id, category, note) "
                 "VALUES (1, 42, 'A', 'first')")
    conn.execute("INSERT INTO jots (skill_run_id, task_id, category, note) "
                 "VALUES (1, 42, 'B', 'second')")
    conn.commit()

    rc, out, err = _run_cli(db_path, "list")
    assert rc == 0, err
    rows = json.loads(out)
    assert len(rows) == 2


def test_cascade_delete_via_skill_run(tmp_path):
    """ON DELETE CASCADE removes jots when the parent skill_run is deleted."""
    db_path, conn = _make_db(tmp_path)
    conn.execute("INSERT INTO jots (skill_run_id, task_id, category, note) "
                 "VALUES (1, 42, 'A', 'jot to be cascaded')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM jots").fetchone()[0] == 1

    # Reopen with FK enforcement so the cascade fires
    conn.close()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM skill_runs WHERE id = 1")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM jots").fetchone()[0] == 0
