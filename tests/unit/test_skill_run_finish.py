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
    cache_read_tokens_in INTEGER,
    cache_write_tokens_in INTEGER,
    uncached_tokens_in INTEGER,
    model TEXT,
    metadata TEXT,
    request_count INTEGER,
    task_id INTEGER,
    transcript_path TEXT,
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


def test_finish_without_transcript_records_missing_transcript_sentinel(db_path, monkeypatch):
    c = sqlite3.connect(str(db_path))
    c.execute(
        "INSERT INTO skill_runs (skill_name, started_at) VALUES (?, ?)",
        ("retro", "2026-05-01 10:00:00"),
    )
    c.commit()
    c.close()

    monkeypatch.setattr(skill_run.lib, "load_pricing", lambda: None)
    monkeypatch.setattr(skill_run.lib, "find_transcript", lambda: None)
    monkeypatch.setattr(
        skill_run.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    exit_code, out, err = _run_main(db_path, monkeypatch, "finish", "1")

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    row = c.execute(
        "SELECT cost_dollars, request_count, model FROM skill_runs WHERE id = 1"
    ).fetchone()
    c.close()

    assert exit_code == 0
    assert "No transcript found" in err
    assert "Model:         (transcript missing)" in out
    assert row["cost_dollars"] == 0
    assert row["request_count"] == 0
    assert row["model"] == "(transcript missing)"


def test_finish_caps_cost_at_first_idle_gap(db_path, monkeypatch):
    c = sqlite3.connect(str(db_path))
    c.execute(
        "INSERT INTO skill_runs (skill_name, started_at) VALUES (?, ?)",
        ("tusk", "2026-05-01 10:00:00"),
    )
    c.commit()
    c.close()

    captured = {}

    def fake_aggregate(transcript_path, started_at, ended_at, *, stop_at_idle_gap=False):
        captured["stop_at_idle_gap"] = stop_at_idle_gap
        return {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_input_tokens": 0,
            "cache_creation_5m_tokens": 0,
            "cache_creation_1h_tokens": 0,
            "cache_read_input_tokens": 5,
            "model": "claude-sonnet-4-6",
            "request_count": 1,
            "user_prompt_tokens": 8,
            "user_prompt_count": 1,
        }

    monkeypatch.setattr(skill_run.lib, "load_pricing", lambda: None)
    monkeypatch.setattr(skill_run.lib, "find_transcript", lambda: "/tmp/transcript.jsonl")
    monkeypatch.setattr(skill_run.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(skill_run.lib, "aggregate_session", fake_aggregate)
    monkeypatch.setattr(skill_run.lib, "compute_cost", lambda totals: 0.0042)
    monkeypatch.setattr(skill_run.lib, "compute_tokens_in", lambda totals: 105)
    monkeypatch.setattr(
        skill_run.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    exit_code, out, err = _run_main(db_path, monkeypatch, "finish", "1")

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    row = c.execute(
        "SELECT cost_dollars, tokens_in, request_count FROM skill_runs WHERE id = 1"
    ).fetchone()
    c.close()

    assert exit_code == 0
    assert err == ""
    assert captured["stop_at_idle_gap"] is True
    assert row["cost_dollars"] == 0.0042
    assert row["tokens_in"] == 105
    assert row["request_count"] == 1
    assert "Requests:      1" in out


def test_start_records_current_transcript_path(db_path, monkeypatch):
    monkeypatch.setattr(skill_run.lib, "find_transcript", lambda: "/tmp/session-a.jsonl")

    exit_code, out, err = _run_main(db_path, monkeypatch, "start", "tusk")

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT transcript_path FROM skill_runs WHERE id = 1").fetchone()
    c.close()

    assert exit_code == 0
    assert err == ""
    assert '"run_id":1' in out
    assert row["transcript_path"] == "/tmp/session-a.jsonl"


def test_finish_prefers_pinned_transcript_over_newest_sibling(db_path, monkeypatch):
    c = sqlite3.connect(str(db_path))
    c.execute(
        "INSERT INTO skill_runs (skill_name, started_at, transcript_path) VALUES (?, ?, ?)",
        ("review-commits", "2026-05-01 10:00:00", "/tmp/session-a.jsonl"),
    )
    c.commit()
    c.close()

    captured = {}

    def fake_aggregate(transcript_path, started_at, ended_at, *, stop_at_idle_gap=False):
        captured["transcript_path"] = transcript_path
        return {
            "input_tokens": 200,
            "output_tokens": 30,
            "cache_creation_input_tokens": 0,
            "cache_creation_5m_tokens": 0,
            "cache_creation_1h_tokens": 0,
            "cache_read_input_tokens": 10,
            "model": "claude-fable-5",
            "request_count": 2,
            "user_prompt_tokens": 12,
            "user_prompt_count": 1,
        }

    monkeypatch.setattr(skill_run.lib, "load_pricing", lambda: None)
    monkeypatch.setattr(skill_run.lib, "find_transcript", lambda: "/tmp/session-b.jsonl")
    monkeypatch.setattr(skill_run.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(skill_run.lib, "aggregate_session", fake_aggregate)
    monkeypatch.setattr(skill_run.lib, "compute_cost", lambda totals: 0.0084)
    monkeypatch.setattr(skill_run.lib, "compute_tokens_in", lambda totals: 210)
    monkeypatch.setattr(
        skill_run.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    exit_code, out, err = _run_main(db_path, monkeypatch, "finish", "1")

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    row = c.execute(
        "SELECT model, tokens_in, request_count FROM skill_runs WHERE id = 1"
    ).fetchone()
    c.close()

    assert exit_code == 0
    assert err == ""
    assert captured["transcript_path"] == "/tmp/session-a.jsonl"
    assert row["model"] == "claude-fable-5"
    assert row["tokens_in"] == 210
    assert row["request_count"] == 2
    assert "Model:         claude-fable-5" in out


def test_finish_falls_back_when_pinned_transcript_is_missing(db_path, monkeypatch):
    c = sqlite3.connect(str(db_path))
    c.execute(
        "INSERT INTO skill_runs (skill_name, started_at, transcript_path) VALUES (?, ?, ?)",
        ("retro", "2026-05-01 10:00:00", "/tmp/missing-session.jsonl"),
    )
    c.commit()
    c.close()

    captured = {}

    def fake_isfile(path):
        return path == "/tmp/newest-session.jsonl"

    def fake_aggregate(transcript_path, started_at, ended_at, *, stop_at_idle_gap=False):
        captured["transcript_path"] = transcript_path
        return {
            "input_tokens": 90,
            "output_tokens": 11,
            "cache_creation_input_tokens": 0,
            "cache_creation_5m_tokens": 0,
            "cache_creation_1h_tokens": 0,
            "cache_read_input_tokens": 3,
            "model": "claude-sonnet-4-6",
            "request_count": 1,
            "user_prompt_tokens": 5,
            "user_prompt_count": 1,
        }

    monkeypatch.setattr(skill_run.lib, "load_pricing", lambda: None)
    monkeypatch.setattr(skill_run.lib, "find_transcript", lambda: "/tmp/newest-session.jsonl")
    monkeypatch.setattr(skill_run.os.path, "isfile", fake_isfile)
    monkeypatch.setattr(skill_run.lib, "aggregate_session", fake_aggregate)
    monkeypatch.setattr(skill_run.lib, "compute_cost", lambda totals: 0.0033)
    monkeypatch.setattr(skill_run.lib, "compute_tokens_in", lambda totals: 93)
    monkeypatch.setattr(
        skill_run.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    exit_code, out, err = _run_main(db_path, monkeypatch, "finish", "1")

    assert exit_code == 0
    assert "Pinned transcript missing" in err
    assert captured["transcript_path"] == "/tmp/newest-session.jsonl"
    assert "Model:         claude-sonnet-4-6" in out


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
