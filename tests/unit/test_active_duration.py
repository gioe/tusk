"""Unit coverage for idle-gap-discounted active duration (issue #1069).

active_seconds approximates real working time by summing consecutive
transcript-event deltas at or below the idle threshold; an overnight pause
contributes nothing, so a session left open reports active well below wall.
"""

import importlib.util
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_pricing_lib", os.path.join(BIN, "tusk-pricing-lib.py")
)
lib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lib)

_ts_spec = importlib.util.spec_from_file_location(
    "tusk_task_summary", os.path.join(BIN, "tusk-task-summary.py")
)
task_summary = importlib.util.module_from_spec(_ts_spec)
_ts_spec.loader.exec_module(task_summary)

T0 = datetime(2026, 6, 11, 1, 0, 0, tzinfo=timezone.utc)


def _ts(offset_seconds):
    return T0 + timedelta(seconds=offset_seconds)


class TestComputeActiveSeconds:
    def test_continuous_session_counts_all_deltas(self):
        # Events every 60s for 10 minutes — all deltas below threshold.
        stamps = [_ts(i * 60) for i in range(11)]
        assert lib.compute_active_seconds(stamps) == 600

    def test_overnight_gap_discounted(self):
        # 20 minutes of work, a ~9h40m idle gap, then 5 more minutes.
        stamps = [_ts(i * 60) for i in range(21)]
        stamps += [_ts(34800 + i * 60) for i in range(6)]
        # 20min before the gap + 5min after; the 9h+ gap contributes 0.
        assert lib.compute_active_seconds(stamps) == 1200 + 300

    def test_gap_exactly_at_threshold_counts(self):
        stamps = [_ts(0), _ts(lib.IDLE_GAP_THRESHOLD_SECONDS)]
        assert lib.compute_active_seconds(stamps) == lib.IDLE_GAP_THRESHOLD_SECONDS

    def test_gap_just_over_threshold_discounted(self):
        stamps = [_ts(0), _ts(lib.IDLE_GAP_THRESHOLD_SECONDS + 1)]
        assert lib.compute_active_seconds(stamps) == 0

    def test_fewer_than_two_events_is_zero(self):
        assert lib.compute_active_seconds([]) == 0
        assert lib.compute_active_seconds([_ts(0)]) == 0

    def test_unsorted_input_is_sorted_first(self):
        stamps = [_ts(120), _ts(0), _ts(60)]
        assert lib.compute_active_seconds(stamps) == 120


class TestAggregateSessionActiveSeconds:
    def _write_transcript(self, tmp_path, entries):
        path = tmp_path / "transcript.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return str(path)

    def _assistant(self, offset, request_id):
        return {
            "type": "assistant",
            "timestamp": _ts(offset).isoformat(),
            "requestId": request_id,
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        }

    def test_active_seconds_in_totals(self, tmp_path):
        entries = [self._assistant(i * 60, f"r{i}") for i in range(11)]
        # overnight gap, then two more requests a minute apart
        entries += [self._assistant(34800 + i * 60, f"late{i}") for i in range(2)]
        transcript = self._write_transcript(tmp_path, entries)
        totals = lib.aggregate_session(transcript, T0, None)
        assert totals["active_seconds"] == 600 + 60

    def test_stop_at_idle_gap_excludes_later_request_tokens(self, tmp_path):
        entries = [
            self._assistant(0, "early"),
            self._assistant(lib.IDLE_GAP_THRESHOLD_SECONDS + 1, "late"),
        ]
        transcript = self._write_transcript(tmp_path, entries)

        totals = lib.aggregate_session(transcript, T0, None, stop_at_idle_gap=True)

        assert totals["request_count"] == 1
        assert totals["input_tokens"] == 10
        assert totals["output_tokens"] == 5
        assert totals["active_seconds"] == 0

    def test_out_of_window_events_excluded(self, tmp_path):
        entries = [self._assistant(i * 60, f"r{i}") for i in range(3)]
        transcript = self._write_transcript(tmp_path, entries)
        # Window starts after the first event — only two in-window events.
        totals = lib.aggregate_session(transcript, _ts(60), None)
        assert totals["active_seconds"] == 60


class TestUpdateSessionStatsActiveSeconds:
    def _make_db(self, with_column=True):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cols = "id INTEGER PRIMARY KEY, tokens_in INTEGER, tokens_out INTEGER, " \
               "cost_dollars REAL, model TEXT, peak_context_tokens INTEGER, " \
               "first_context_tokens INTEGER, last_context_tokens INTEGER, " \
               "context_window INTEGER, request_count INTEGER, " \
               "cache_read_tokens_in INTEGER, cache_write_tokens_in INTEGER, " \
               "uncached_tokens_in INTEGER, duration_seconds INTEGER"
        if with_column:
            cols += ", active_seconds INTEGER"
        conn.execute(f"CREATE TABLE task_sessions ({cols})")
        conn.execute("INSERT INTO task_sessions (id, duration_seconds) VALUES (1, 33600)")
        return conn

    def _totals(self):
        return {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_5m_tokens": 0,
            "cache_creation_1h_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "model": "",
            "request_count": 2,
            "active_seconds": 1500,
        }

    def test_writes_active_seconds(self):
        conn = self._make_db()
        lib.update_session_stats(conn, 1, self._totals())
        assert conn.execute(
            "SELECT active_seconds FROM task_sessions WHERE id = 1"
        ).fetchone()[0] == 1500

    def test_pre_migration_schema_degrades_silently(self):
        conn = self._make_db(with_column=False)
        lib.update_session_stats(conn, 1, self._totals())  # must not raise


class TestTaskSummaryFallback:
    def _make_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE task_sessions (id INTEGER PRIMARY KEY, task_id INTEGER, "
            "started_at TEXT, ended_at TEXT, duration_seconds INTEGER, "
            "active_seconds INTEGER)"
        )
        return conn

    def test_prefers_active_seconds_with_per_row_fallback(self):
        conn = self._make_db()
        # New-style row with computed active, legacy row with NULL active.
        conn.execute(
            "INSERT INTO task_sessions VALUES (1, 9, '2026-06-11 01:00:00', "
            "'2026-06-11 10:20:00', 33600, 1500)"
        )
        conn.execute(
            "INSERT INTO task_sessions VALUES (2, 9, '2026-06-11 11:00:00', "
            "'2026-06-11 11:30:00', 1800, NULL)"
        )
        dur = task_summary.fetch_duration(
            conn, 9, {"closed_at": "2026-06-11 11:30:00"}
        )
        assert dur["active_seconds"] == 1500 + 1800
        assert dur["session_count"] == 2

    def test_pre_migration_schema_falls_back_to_duration(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE task_sessions (id INTEGER PRIMARY KEY, task_id INTEGER, "
            "started_at TEXT, ended_at TEXT, duration_seconds INTEGER)"
        )
        conn.execute(
            "INSERT INTO task_sessions VALUES (1, 9, '2026-06-11 01:00:00', "
            "'2026-06-11 10:20:00', 33600)"
        )
        dur = task_summary.fetch_duration(
            conn, 9, {"closed_at": "2026-06-11 10:20:00"}
        )
        assert dur["active_seconds"] == 33600
