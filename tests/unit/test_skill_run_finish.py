"""Unit tests for `tusk skill-run finish` diagnostics.

Loads tusk-skill-run.py via importlib and exercises the finish dispatcher
against a temporary SQLite DB. The regression target is silent nonzero finish
failures: even if an inner cleanup path exits with no message, the CLI must
leave a human-readable diagnostic.
"""

import importlib.util
import io
import os
import sqlite3
import sys
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_skill_run():
    bin_dir = os.path.join(REPO_ROOT, "bin")
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    path = os.path.join(bin_dir, "tusk-skill-run.py")
    spec = importlib.util.spec_from_file_location("tusk_skill_run_finish_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


skill_run = _load_skill_run()


_SKILL_RUNS_TABLE = """
CREATE TABLE skill_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT,
    cost_dollars REAL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    model TEXT,
    metadata TEXT,
    request_count INTEGER,
    task_id INTEGER,
    user_prompt_tokens INTEGER,
    user_prompt_count INTEGER
);
"""


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "tasks.db"
    c = sqlite3.connect(str(p))
    c.executescript(_SKILL_RUNS_TABLE)
    c.commit()
    c.close()
    return p


def _run_main(db_path, monkeypatch, *extra_args):
    monkeypatch.setattr(sys, "argv", ["tusk-skill-run", str(db_path), "", *extra_args])
    out, err = io.StringIO(), io.StringIO()
    exit_code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            skill_run.main()
        except SystemExit as e:
            exit_code = e.code if e.code is not None else 0
    return exit_code, out.getvalue(), err.getvalue()


def test_finish_missing_run_id_names_the_unavailable_row(db_path, monkeypatch):
    exit_code, out, err = _run_main(db_path, monkeypatch, "finish", "99999")

    assert exit_code == 1
    assert out == ""
    assert "No skill run found with id 99999" in err


def test_finish_silent_inner_exit_gets_fallback_diagnostic(db_path, monkeypatch):
    def silent_failure(conn, run_id, metadata, db_path):
        raise SystemExit(5)

    monkeypatch.setattr(skill_run, "cmd_finish", silent_failure)

    exit_code, out, err = _run_main(db_path, monkeypatch, "finish", "123")

    assert exit_code == 5
    assert out == ""
    assert "skill-run finish failed with exit code 5" in err
    assert "run_id 123" in err


def test_finish_already_finished_row_warns_and_exits_zero(db_path, monkeypatch):
    c = sqlite3.connect(str(db_path))
    c.execute(
        "INSERT INTO skill_runs"
        " (skill_name, started_at, ended_at, cost_dollars, tokens_in, tokens_out, model, metadata)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "tusk",
            "2026-05-01 10:00:00",
            "2026-05-01 10:05:00",
            0.12,
            100,
            20,
            "claude-sonnet-4-6",
            None,
        ),
    )
    c.commit()
    c.close()

    monkeypatch.setattr(skill_run.lib, "load_pricing", lambda: None)
    monkeypatch.setattr(skill_run.lib, "find_transcript", lambda: None)
    monkeypatch.setattr(
        skill_run.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr=""),
    )

    exit_code, out, err = _run_main(db_path, monkeypatch, "finish", "1")

    assert exit_code == 0
    assert "already finished" in err
    assert "Skill run 1 (tusk) finished:" in out


def test_list_task_id_shows_closed_task_runs(db_path, monkeypatch):
    c = sqlite3.connect(str(db_path))
    c.execute(
        "INSERT INTO skill_runs"
        " (skill_name, task_id, started_at, ended_at, cost_dollars, tokens_in, tokens_out, model, metadata)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "tusk",
            2481,
            "2026-05-01 10:00:00",
            "2026-05-01 10:05:00",
            0.12,
            100,
            20,
            "claude-sonnet-4-6",
            None,
        ),
    )
    c.commit()
    c.close()

    exit_code, out, err = _run_main(db_path, monkeypatch, "list", "--task-id", "2481")

    assert exit_code == 0
    assert err == ""
    assert "TASK-2481" in out
    assert "No skill runs recorded yet" not in out


def test_list_task_id_empty_names_filter(db_path, monkeypatch):
    exit_code, out, err = _run_main(db_path, monkeypatch, "list", "--task-id", "2481")

    assert exit_code == 0
    assert err == ""
    assert "No skill runs recorded for task_id 2481." in out
