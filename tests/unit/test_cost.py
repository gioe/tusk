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
    cost_dollars REAL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    request_count INTEGER,
    model TEXT,
    telemetry_status TEXT
);
CREATE TABLE skill_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    task_id INTEGER,
    started_at TEXT,
    ended_at TEXT,
    cost_dollars REAL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    request_count INTEGER,
    model TEXT,
    telemetry_status TEXT,
    metadata TEXT
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


def test_version_1229_zero_usage_rows_are_unavailable_not_free(tmp_path):
    _, conn = _make_db(tmp_path)
    conn.execute(
        """INSERT INTO task_sessions
           (task_id, started_at, ended_at, cost_dollars, telemetry_status)
           VALUES (3756, '2026-07-19 10:00:00', '2026-07-19 12:00:00',
                   NULL, NULL)"""
    )
    conn.executemany(
        """INSERT INTO skill_runs
           (skill_name, task_id, started_at, ended_at, cost_dollars,
            tokens_in, tokens_out, request_count, telemetry_status, model)
           VALUES (?, 3756, ?, ?, 0, 0, 0, 0, NULL, '(unknown)')""",
        [
            ("tusk", "2026-07-19 10:00:01", "2026-07-19 11:00:00"),
            ("review-commits", "2026-07-19 11:01:00", "2026-07-19 11:15:00"),
        ],
    )
    conn.commit()

    summary = cost.build_cost_summary(conn)
    rendered = cost._render_text(summary)

    assert summary["cost_status"] == "unavailable"
    assert summary["known_subtotal_dollars"] == 0.0
    assert summary["unavailable_completed_windows"] == 2
    assert summary["task_sessions"]["unavailable"] == 1
    assert summary["skill_runs"]["deduped_tusk_unavailable"] == 1
    assert summary["skill_runs"]["unavailable"] == 1
    assert rendered.startswith("Total project cost: unavailable (2 completed windows)")
    assert "$0.0000" not in rendered


def test_captured_zero_cost_is_known(tmp_path):
    _, conn = _make_db(tmp_path)
    conn.execute(
        """INSERT INTO skill_runs
           (skill_name, task_id, started_at, ended_at, cost_dollars,
            tokens_in, tokens_out, request_count, telemetry_status, model)
           VALUES ('retro', 1, '2026-07-19 10:00:00', '2026-07-19 10:01:00',
                   0, 0, 0, 0, 'captured', 'gpt-test')"""
    )
    conn.commit()

    summary = cost.build_cost_summary(conn)

    assert summary["cost_status"] == "complete"
    assert summary["skill_runs"]["included_costed"] == 1
    assert cost._render_text(summary).startswith("Total project cost: $0.0000")


def test_pending_and_cancelled_rows_are_excluded(tmp_path):
    _, conn = _make_db(tmp_path)
    conn.executemany(
        """INSERT INTO skill_runs
           (skill_name, task_id, started_at, ended_at, cost_dollars,
            tokens_in, tokens_out, request_count, telemetry_status, model, metadata)
           VALUES (?, 1, ?, ?, ?, 0, 0, 0, ?, ?, NULL)""",
        [
            ("tusk", "2026-07-19 10:00:00", None, None, "pending", None),
            ("review-commits", "2026-07-19 10:01:00", "2026-07-19 10:02:00", 0, "cancelled", ""),
            ("retro", "2026-07-19 10:03:00", "2026-07-19 10:04:00", 0, None, ""),
        ],
    )
    conn.commit()

    summary = cost.build_cost_summary(conn)

    assert summary["cost_status"] == "no_data"
    assert summary["unavailable_completed_windows"] == 0
    assert summary["skill_runs"]["excluded"] == 3
    assert cost._render_text(summary).startswith(
        "Total project cost: unavailable (no completed accounting)"
    )


def test_mixed_known_and_unavailable_rows_report_known_subtotal(tmp_path):
    _, conn = _make_db(tmp_path)
    conn.executemany(
        """INSERT INTO skill_runs
           (skill_name, task_id, started_at, ended_at, cost_dollars,
            tokens_in, tokens_out, request_count, telemetry_status, model)
           VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            ("review-commits", "2026-07-19 10:00:00", "2026-07-19 10:10:00", 0.25, 10, 5, 1, "captured", "gpt-test"),
            ("retro", "2026-07-19 10:11:00", "2026-07-19 10:12:00", 0, 0, 0, 0, None, "(unknown)"),
        ],
    )
    conn.commit()

    summary = cost.build_cost_summary(conn)

    assert summary["cost_status"] == "partial"
    assert summary["known_subtotal_dollars"] == 0.25
    assert summary["unavailable_completed_windows"] == 1
    assert cost._render_text(summary).startswith(
        "Known project cost subtotal: $0.2500 (1 completed window unavailable)"
    )


def test_tusk_shadow_dedupe_preserves_known_skill_over_unavailable_session(tmp_path):
    _, conn = _make_db(tmp_path)
    conn.execute(
        """INSERT INTO task_sessions
           (task_id, started_at, ended_at, cost_dollars, telemetry_status)
           VALUES (1, '2026-07-19 10:00:00', '2026-07-19 10:30:00',
                   NULL, 'unpriced_model')"""
    )
    conn.execute(
        """INSERT INTO skill_runs
           (skill_name, task_id, started_at, ended_at, cost_dollars,
            telemetry_status)
           VALUES ('tusk', 1, '2026-07-19 10:00:01', '2026-07-19 10:30:00',
                   1.5, 'captured')"""
    )
    conn.commit()

    summary = cost.build_cost_summary(conn)

    assert summary["cost_status"] == "partial"
    assert summary["known_subtotal_dollars"] == 1.5
    assert summary["task_sessions"]["unavailable"] == 1
    assert summary["skill_runs"]["included_costed"] == 1
    assert summary["skill_runs"]["deduped_tusk_shadows"] == 0


def test_tusk_shadow_dedupe_drops_unavailable_skill_under_known_session(tmp_path):
    _, conn = _make_db(tmp_path)
    conn.execute(
        """INSERT INTO task_sessions
           (task_id, started_at, ended_at, cost_dollars, telemetry_status)
           VALUES (1, '2026-07-19 10:00:00', '2026-07-19 10:30:00',
                   2.0, 'captured')"""
    )
    conn.execute(
        """INSERT INTO skill_runs
           (skill_name, task_id, started_at, ended_at, cost_dollars,
            tokens_in, tokens_out, request_count, telemetry_status, model)
           VALUES ('tusk', 1, '2026-07-19 10:00:01', '2026-07-19 10:30:00',
                   0, 0, 0, 0, NULL, '(unknown)')"""
    )
    conn.commit()

    summary = cost.build_cost_summary(conn)

    assert summary["cost_status"] == "complete"
    assert summary["known_subtotal_dollars"] == 2.0
    assert summary["skill_runs"]["deduped_tusk_shadows"] == 1
    assert summary["skill_runs"]["deduped_tusk_unavailable"] == 1
    assert summary["unavailable_completed_windows"] == 0


def test_historical_schema_without_telemetry_columns_still_works(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE task_sessions (
            id INTEGER PRIMARY KEY, task_id INTEGER, started_at TEXT,
            ended_at TEXT, cost_dollars REAL
        );
        CREATE TABLE skill_runs (
            id INTEGER PRIMARY KEY, skill_name TEXT, task_id INTEGER,
            started_at TEXT, ended_at TEXT, cost_dollars REAL
        );
        INSERT INTO task_sessions VALUES
            (1, 1, '2026-07-19 10:00:00', '2026-07-19 11:00:00', NULL);
        INSERT INTO skill_runs VALUES
            (1, 'retro', 1, '2026-07-19 11:01:00', '2026-07-19 11:02:00', 0);
        """
    )

    summary = cost.build_cost_summary(conn)

    assert summary["cost_status"] == "unavailable"
    assert summary["unavailable_completed_windows"] == 2
