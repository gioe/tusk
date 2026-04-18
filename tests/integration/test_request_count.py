"""Integration test for migration 49: task_sessions.request_count is persisted.

Verifies that `tusk session-stats` writes the deduplicated Claude API
requestId count into the new request_count column on task_sessions. The
fixture transcript contains three unique requestIds plus one duplicate
entry (streaming chunks share a requestId) — aggregate_session() must
dedupe by requestId, so the persisted count is 3, not 4.
"""

import json
import os
import sqlite3
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _assistant_entry(request_id: str, timestamp: str, *, input_tokens: int = 100, output_tokens: int = 50) -> dict:
    """Build a minimal assistant-message JSONL entry aggregate_session() accepts."""
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "requestId": request_id,
        "message": {
            "model": "claude-sonnet-4-6",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }


@pytest.fixture()
def transcript_with_duplicate(tmp_path):
    """Write a JSONL transcript with 3 unique requestIds plus one duplicate."""
    entries = [
        _assistant_entry("req-a", "2026-04-18T12:00:00Z"),
        _assistant_entry("req-b", "2026-04-18T12:00:10Z"),
        _assistant_entry("req-b", "2026-04-18T12:00:11Z"),  # duplicate — same requestId
        _assistant_entry("req-c", "2026-04-18T12:00:20Z"),
    ]
    path = tmp_path / "transcript.jsonl"
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return str(path)


def test_session_stats_persists_deduped_request_count(db_path, transcript_with_duplicate):
    """tusk session-stats writes request_count matching the unique requestId count."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score)"
            " VALUES ('rc test', 'In Progress', 'feature', 'Medium', 'S', 50)"
        )
        task_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, ended_at)"
            " VALUES (?, '2026-04-18 11:59:00', '2026-04-18 12:01:00')",
            (task_id,),
        )
        session_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    result = subprocess.run(
        [TUSK_BIN, "session-stats", str(session_id), transcript_with_duplicate],
        capture_output=True,
        text=True,
        env={**os.environ, "TUSK_DB": str(db_path)},
    )
    assert result.returncode == 0, (
        f"session-stats failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT request_count FROM task_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "session row missing"
    assert row[0] == 3, f"expected 3 unique requestIds (1 duplicate deduped), got {row[0]}"


def test_task_metrics_exposes_total_request_count(db_path, transcript_with_duplicate):
    """task_metrics.total_request_count aggregates per-task across sessions."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score)"
            " VALUES ('agg test', 'In Progress', 'feature', 'Medium', 'S', 50)"
        )
        task_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO task_sessions (task_id, started_at, ended_at)"
            " VALUES (?, '2026-04-18 11:59:00', '2026-04-18 12:01:00')",
            (task_id,),
        )
        session_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    subprocess.run(
        [TUSK_BIN, "session-stats", str(session_id), transcript_with_duplicate],
        capture_output=True,
        text=True,
        env={**os.environ, "TUSK_DB": str(db_path)},
        check=True,
    )

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT total_request_count FROM task_metrics WHERE id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == 3
