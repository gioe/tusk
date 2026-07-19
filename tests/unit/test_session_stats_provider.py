"""Provider-aware task-session transcript attribution regressions."""

import importlib.util
import io
import os
import sqlite3
import sys
from contextlib import redirect_stderr, redirect_stdout


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


def _load_module():
    if BIN not in sys.path:
        sys.path.insert(0, BIN)
    spec = importlib.util.spec_from_file_location(
        "tusk_session_stats_provider_test", os.path.join(BIN, "tusk-session-stats.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


session_stats = _load_module()


def _db(tmp_path, *, path=None, provider="codex"):
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE task_sessions (
            id INTEGER PRIMARY KEY, started_at TEXT, ended_at TEXT,
            transcript_path TEXT, transcript_provider TEXT, telemetry_status TEXT,
            model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_dollars REAL
        )"""
    )
    conn.execute(
        "INSERT INTO task_sessions VALUES (1, '2026-07-19 12:00:00', NULL, ?, ?, NULL, NULL, NULL, NULL, NULL)",
        (path, provider),
    )
    conn.commit()
    conn.close()
    return db_path


def _run(db_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["tusk-session-stats", str(db_path), "", "1"])
    monkeypatch.setattr(session_stats.lib, "load_pricing", lambda: None)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        session_stats.main()
    return out.getvalue(), err.getvalue()


def test_session_stats_reuses_pinned_codex_transcript(tmp_path, monkeypatch):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("{}\n")
    db_path = _db(tmp_path, path=str(transcript))
    captured = {}

    def aggregate(path, started_at, ended_at):
        captured["path"] = path
        return {
            "request_count": 1,
            "input_tokens": 3,
            "output_tokens": 2,
            "cache_creation_5m_tokens": 0,
            "cache_creation_1h_tokens": 0,
            "cache_read_input_tokens": 0,
            "model": "gpt-test",
        }

    monkeypatch.setattr(session_stats.lib, "aggregate_session", aggregate)
    monkeypatch.setattr(session_stats.lib, "compute_tokens_in", lambda totals: 3)
    monkeypatch.setattr(session_stats.lib, "optional_cost", lambda totals: None)
    monkeypatch.setattr(session_stats.lib, "update_session_stats", lambda conn, sid, totals: None)

    out, err = _run(db_path, monkeypatch)

    assert captured["path"] == str(transcript)
    assert "unavailable (unpriced model)" in out
    assert err == ""


def test_missing_pinned_provider_never_crosses_to_claude(tmp_path, monkeypatch):
    db_path = _db(tmp_path, provider="codex")
    requested = []
    monkeypatch.setattr(
        session_stats.lib,
        "find_transcript",
        lambda **kwargs: requested.append(kwargs["provider"]) or None,
    )

    _, err = _run(db_path, monkeypatch)

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT telemetry_status, tokens_in, cost_dollars FROM task_sessions WHERE id = 1"
    ).fetchone()
    conn.close()
    assert requested == ["codex"]
    assert row == ("transcript_missing", None, None)
    assert "No codex transcript found" in err
