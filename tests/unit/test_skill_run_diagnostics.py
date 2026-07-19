"""Unit tests for `tusk skill-run` start/cancel diagnostics.

Companion to test_skill_run_finish.py; covers the start and cancel error paths
that previously bypassed the finish-only fallback diagnostic wrapper.

Regression targets:
  - Issue #789: `skill-run start --task-id <missing>` raised a bare
    `sqlite3.IntegrityError` traceback ("FOREIGN KEY constraint failed")
    instead of naming the missing task id.
  - Issue #775: any uncaught exception inside cmd_start / cmd_cancel /
    cmd_finish was returned to the caller with no diagnostic context. The
    cluster fix (issue #785's silent-exit guard) only added a generic
    "exited N with no diagnostic output" footer; this test guards that the
    inner command produces an actionable message in the first place.
"""

import importlib.util
import io
import os
import sqlite3
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_skill_run():
    bin_dir = os.path.join(REPO_ROOT, "bin")
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    path = os.path.join(bin_dir, "tusk-skill-run.py")
    spec = importlib.util.spec_from_file_location("tusk_skill_run_diagnostics_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


skill_run = _load_skill_run()


_FK_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL
);
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
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    transcript_path TEXT,
    transcript_provider TEXT,
    telemetry_status TEXT,
    user_prompt_tokens INTEGER,
    user_prompt_count INTEGER
);
"""


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "tasks.db"
    c = sqlite3.connect(str(p))
    c.executescript(_FK_SCHEMA)
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


# ── start: FK violation when --task-id references a missing task ──────


def test_start_with_missing_task_id_names_the_missing_id(db_path, monkeypatch):
    """Issue #789 regression: the worst-offender path on this cluster."""
    exit_code, out, err = _run_main(
        db_path, monkeypatch, "start", "retro", "--task-id", "99999"
    )

    assert exit_code == 1
    assert out == ""
    # The targeted in-cmd_start handler names the missing task id explicitly.
    assert "task_id 99999 does not exist in tasks" in err
    assert "retro" in err
    # The dispatcher wrapper adds a second diagnostic line for symmetry with finish.
    assert "skill-run start failed with exit code 1" in err
    # No bare Python traceback should leak through.
    assert "Traceback" not in err
    assert "FOREIGN KEY constraint failed" not in err.split("\n", 1)[0]


def test_start_with_valid_task_id_still_succeeds(db_path, monkeypatch):
    """Guard against the FK-violation handler being overly broad."""
    c = sqlite3.connect(str(db_path))
    c.execute("INSERT INTO tasks (summary) VALUES (?)", ("real task",))
    c.commit()
    real_task_id = c.execute("SELECT id FROM tasks").fetchone()[0]
    c.close()

    exit_code, out, err = _run_main(
        db_path, monkeypatch, "start", "retro", "--task-id", str(real_task_id)
    )

    assert exit_code == 0
    assert err == ""
    # cmd_start emits one JSON line on stdout for the new run.
    assert '"task_id":' in out
    assert f'"task_id":{real_task_id}' in out.replace(" ", "")


# ── start: uncaught exception is converted to an actionable message ────


def test_start_uncaught_exception_produces_diagnostic(db_path, monkeypatch):
    """Mirror of test_finish_silent_inner_exit_gets_fallback_diagnostic for start."""

    def boom(conn, skill_name, task_id=None):
        raise RuntimeError("upstream cost-tracking blew up")

    monkeypatch.setattr(skill_run, "cmd_start", boom)

    exit_code, out, err = _run_main(db_path, monkeypatch, "start", "retro")

    assert exit_code == 1
    assert out == ""
    # The catch-all top-level handler names the subcommand and the exception type.
    assert "skill-run start crashed with RuntimeError" in err
    assert "upstream cost-tracking blew up" in err


# ── cancel: uncaught exception is converted to an actionable message ───


def test_cancel_uncaught_exception_produces_diagnostic(db_path, monkeypatch):
    def boom(conn, run_id):
        raise RuntimeError("cancel-time db corruption")

    monkeypatch.setattr(skill_run, "cmd_cancel", boom)

    exit_code, out, err = _run_main(db_path, monkeypatch, "cancel", "1")

    assert exit_code == 1
    assert out == ""
    assert "skill-run cancel crashed with RuntimeError" in err
    assert "cancel-time db corruption" in err


def test_cancel_systemexit_gets_fallback_diagnostic(db_path, monkeypatch):
    """Mirrors the SystemExit-wrapper behavior already tested for finish."""

    def silent_failure(conn, run_id):
        raise SystemExit(7)

    monkeypatch.setattr(skill_run, "cmd_cancel", silent_failure)

    exit_code, out, err = _run_main(db_path, monkeypatch, "cancel", "42")

    assert exit_code == 7
    assert out == ""
    assert "skill-run cancel failed with exit code 7" in err
    assert "run_id 42" in err
