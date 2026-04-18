"""Unit tests for --help / -h handling in bin/tusk-skill-run.py.

Regression coverage for TASK-96: before the fix, `tusk skill-run --help`
exited 1 with 'unknown subcommand', `tusk skill-run start --help` inserted
a stray skill_runs row with skill_name='--help', and `tusk skill-run
finish/cancel --help` exited 1 with 'run_id must be an integer'. The fix
routes --help / -h at subcommand and positional-arg positions to the
corresponding usage line on stdout with exit 0, and inserts nothing.

Loads tusk-skill-run.py via importlib (hyphenated filename) and exercises
main() as a top-level dispatcher, matching the pattern in
test_skill_run_cancel.py.
"""

import importlib.util
import io
import os
import sqlite3
import sys

import pytest
from contextlib import redirect_stderr, redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_skill_run():
    bin_dir = os.path.join(REPO_ROOT, "bin")
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    path = os.path.join(bin_dir, "tusk-skill-run.py")
    spec = importlib.util.spec_from_file_location("tusk_skill_run_help_under_test", path)
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
    task_id INTEGER
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
    """Invoke skill_run.main() with the standard argv layout and capture io."""
    monkeypatch.setattr(sys, "argv", ["tusk-skill-run", str(db_path), "", *extra_args])
    out, err = io.StringIO(), io.StringIO()
    exit_code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            skill_run.main()
        except SystemExit as e:
            exit_code = e.code if e.code is not None else 0
    return exit_code, out.getvalue(), err.getvalue()


def _row_count(db_path):
    c = sqlite3.connect(str(db_path))
    try:
        return c.execute("SELECT COUNT(*) FROM skill_runs").fetchone()[0]
    finally:
        c.close()


class TestTopLevelHelp:
    """Covers criterion 408: `tusk skill-run --help` (top-level)."""

    def test_double_dash_help_prints_combined_usage_and_exits_zero(self, db_path, monkeypatch):
        exit_code, out, err = _run_main(db_path, monkeypatch, "--help")
        assert exit_code == 0
        # Combined usage mentions every subcommand so users discover the full surface.
        assert "Usage: tusk skill-run" in out
        assert "start" in out and "finish" in out and "cancel" in out and "list" in out
        # Usage goes to stdout for --help (not the stderr error path).
        assert err == ""

    def test_short_dash_h_prints_combined_usage_and_exits_zero(self, db_path, monkeypatch):
        exit_code, out, err = _run_main(db_path, monkeypatch, "-h")
        assert exit_code == 0
        assert "Usage: tusk skill-run" in out


class TestStartHelp:
    """Covers criterion 406: `tusk skill-run start --help` must not insert a row."""

    def test_start_double_dash_help_prints_usage_and_inserts_nothing(self, db_path, monkeypatch):
        assert _row_count(db_path) == 0
        exit_code, out, err = _run_main(db_path, monkeypatch, "start", "--help")
        assert exit_code == 0
        assert "Usage: tusk skill-run start" in out
        assert err == ""
        # The bug was that --help was interpreted as skill_name and a row was inserted.
        assert _row_count(db_path) == 0

    def test_start_short_dash_h_prints_usage_and_inserts_nothing(self, db_path, monkeypatch):
        exit_code, out, err = _run_main(db_path, monkeypatch, "start", "-h")
        assert exit_code == 0
        assert "Usage: tusk skill-run start" in out
        assert _row_count(db_path) == 0


class TestFinishCancelHelp:
    """Covers criterion 407: finish/cancel --help print usage and exit 0 instead of an integer error."""

    def test_finish_double_dash_help_prints_usage_and_exits_zero(self, db_path, monkeypatch):
        exit_code, out, err = _run_main(db_path, monkeypatch, "finish", "--help")
        assert exit_code == 0
        assert "Usage: tusk skill-run finish" in out
        # Before the fix, this branch reported 'run_id must be an integer'.
        assert "must be an integer" not in err

    def test_finish_short_dash_h_prints_usage_and_exits_zero(self, db_path, monkeypatch):
        exit_code, out, err = _run_main(db_path, monkeypatch, "finish", "-h")
        assert exit_code == 0
        assert "Usage: tusk skill-run finish" in out

    def test_cancel_double_dash_help_prints_usage_and_exits_zero(self, db_path, monkeypatch):
        exit_code, out, err = _run_main(db_path, monkeypatch, "cancel", "--help")
        assert exit_code == 0
        assert "Usage: tusk skill-run cancel" in out
        assert "must be an integer" not in err

    def test_cancel_short_dash_h_prints_usage_and_exits_zero(self, db_path, monkeypatch):
        exit_code, out, err = _run_main(db_path, monkeypatch, "cancel", "-h")
        assert exit_code == 0
        assert "Usage: tusk skill-run cancel" in out
