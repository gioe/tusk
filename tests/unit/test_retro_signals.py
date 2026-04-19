"""Unit tests for tusk-retro-signals.py.

Covers each of the six signal branches with seeded fixtures, plus empty-state
shape, output compactness, TASK-N prefix handling, and the not-found exit path.
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_retro_signals",
    os.path.join(BIN, "tusk-retro-signals.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ── schema fixture ────────────────────────────────────────────────────
# Minimal subset of the real schema — only the columns this script queries.
# Not meant to mirror bin/tusk; no schema-sync guard is needed (see
# CLAUDE.md "macOS case-insensitive filesystem" section for the sync-guard
# discussion — only test_workflow / test_dashboard_data / test_skill_run_cancel
# fixtures mirror the canonical tables).

_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    summary TEXT,
    description TEXT,
    status TEXT DEFAULT 'To Do',
    complexity TEXT,
    fixes_task_id INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE task_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL
);
CREATE TABLE task_status_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    changed_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE task_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    commit_hash TEXT,
    commit_message TEXT,
    files_changed TEXT,
    next_steps TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE acceptance_criteria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    criterion TEXT,
    is_completed INTEGER DEFAULT 0,
    is_deferred INTEGER DEFAULT 0,
    skip_note TEXT
);
CREATE TABLE code_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    status TEXT DEFAULT 'pending'
);
CREATE TABLE review_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL,
    file_path TEXT,
    category TEXT,
    severity TEXT,
    comment TEXT NOT NULL,
    resolution TEXT,
    deferred_task_id INTEGER
);
CREATE TABLE tool_call_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    tool_name TEXT NOT NULL,
    call_count INTEGER NOT NULL DEFAULT 0,
    total_cost REAL NOT NULL DEFAULT 0.0
);
"""


def _make_db(tmp_path, task_id=1, complexity="M"):
    tusk_dir = tmp_path / "tusk"
    tusk_dir.mkdir(exist_ok=True)
    db_path = str(tusk_dir / "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO tasks (id, summary, complexity) VALUES (?, ?, ?)",
        (task_id, f"Task {task_id}", complexity),
    )
    conn.commit()
    return db_path, conn


def _run_main(db_path, task_id, config_path="fake.json"):
    result = subprocess.run(
        [sys.executable, os.path.join(BIN, "tusk-retro-signals.py"),
         db_path, config_path, str(task_id)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode, result.stdout, result.stderr


# ── helpers ───────────────────────────────────────────────────────────


class TestCompact:
    def test_under_limit_unchanged(self):
        assert mod._compact("short text", 80) == "short text"

    def test_over_limit_truncated_with_ellipsis(self):
        long = "a" * 200
        out = mod._compact(long, 50)
        assert len(out) == 50
        assert out.endswith("…")

    def test_none_and_empty(self):
        assert mod._compact(None, 80) == ""
        assert mod._compact("", 80) == ""

    def test_strips_whitespace(self):
        assert mod._compact("  hello  ", 80) == "hello"


class TestResolveTaskId:
    def test_plain_integer(self):
        assert mod._resolve_task_id("42") == 42

    def test_task_prefix(self):
        assert mod._resolve_task_id("TASK-42") == 42

    def test_task_prefix_lowercase(self):
        assert mod._resolve_task_id("task-42") == 42

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            mod._resolve_task_id("not-an-id")


# ── reopen_count ──────────────────────────────────────────────────────


class TestReopenCount:
    def test_zero_when_no_transitions(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1)
        conn.close()
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        assert mod.fetch_reopen_count(c, 1) == 0

    def test_counts_transitions_back_to_todo(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1)
        # Two reopens plus an unrelated In Progress→Done that should not count.
        conn.executemany(
            "INSERT INTO task_status_transitions (task_id, from_status, to_status) VALUES (?, ?, ?)",
            [
                (1, "In Progress", "To Do"),   # mid-task rework
                (1, "Done", "To Do"),          # post-done reopen
                (1, "In Progress", "Done"),    # not a reopen
                (2, "In Progress", "To Do"),   # different task — ignored
            ],
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        assert mod.fetch_reopen_count(conn, 1) == 2


# ── rework_chain ──────────────────────────────────────────────────────


class TestReworkChain:
    def test_empty_when_no_fixes_links(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1)
        conn.close()
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        chain = mod.fetch_rework_chain(c, 1)
        assert chain == {"fixes": [], "fixed_by": []}

    def test_both_directions(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=10)
        # Task 10 fixes task 5; tasks 20 and 21 fix task 10.
        conn.executescript("""
            INSERT INTO tasks (id, summary, status) VALUES (5, 'parent', 'Done');
            INSERT INTO tasks (id, summary, status, fixes_task_id) VALUES (20, 'follow-up a', 'Done', 10);
            INSERT INTO tasks (id, summary, status, fixes_task_id) VALUES (21, 'follow-up b', 'To Do', 10);
            UPDATE tasks SET fixes_task_id = 5 WHERE id = 10;
        """)
        conn.commit()
        conn.row_factory = sqlite3.Row
        chain = mod.fetch_rework_chain(conn, 10)
        assert chain["fixes"] == [{"id": 5, "summary": "parent", "status": "Done"}]
        assert {t["id"] for t in chain["fixed_by"]} == {20, 21}
        assert chain["fixed_by"][0]["id"] == 20  # ordered by created_at, id


# ── review_themes ─────────────────────────────────────────────────────


class TestReviewThemes:
    def test_empty_when_no_reviews(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1)
        conn.close()
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        assert mod.fetch_review_themes(c, 1) == []

    def test_filters_below_recurrence_threshold(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1)
        conn.executescript("""
            INSERT INTO code_reviews (id, task_id) VALUES (1, 1);
            INSERT INTO review_comments (review_id, category, severity, comment)
                VALUES (1, 'style', 'low', 'single occurrence — should drop');
        """)
        conn.commit()
        conn.row_factory = sqlite3.Row
        assert mod.fetch_review_themes(conn, 1) == []

    def test_groups_recurring_themes_with_compact_sample(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1)
        long_body = "x" * 500  # proves samples are truncated, not passed through raw
        conn.executescript(f"""
            INSERT INTO code_reviews (id, task_id) VALUES (1, 1);
            INSERT INTO code_reviews (id, task_id) VALUES (2, 1);
            INSERT INTO review_comments (review_id, category, severity, comment)
                VALUES (1, 'correctness', 'high', '{long_body}');
            INSERT INTO review_comments (review_id, category, severity, comment)
                VALUES (1, 'correctness', 'high', 'second occurrence');
            INSERT INTO review_comments (review_id, category, severity, comment)
                VALUES (2, 'correctness', 'high', 'third occurrence');
            INSERT INTO review_comments (review_id, category, severity, comment)
                VALUES (1, 'style', 'low', 'once');
        """)
        conn.commit()
        conn.row_factory = sqlite3.Row
        themes = mod.fetch_review_themes(conn, 1)
        assert len(themes) == 1  # only the recurring pair survives
        t = themes[0]
        assert t["category"] == "correctness"
        assert t["severity"] == "high"
        assert t["count"] == 3
        assert len(t["sample"]) <= mod.REVIEW_SAMPLE_MAX_CHARS
        assert t["sample"].endswith("…")  # was truncated


# ── deferred_review_comments ──────────────────────────────────────────


class TestDeferredReviewComments:
    def test_empty_when_no_reviews(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1)
        conn.close()
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        assert mod.fetch_deferred_review_comments(c, 1) == []

    def test_only_deferred_resolution_is_returned(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1)
        conn.executescript("""
            INSERT INTO code_reviews (id, task_id) VALUES (1, 1);
            INSERT INTO review_comments
                (review_id, file_path, category, severity, comment, resolution, deferred_task_id)
                VALUES (1, 'src/a.py', 'correctness', 'high', 'fixed in place', 'fixed', NULL);
            INSERT INTO review_comments
                (review_id, file_path, category, severity, comment, resolution, deferred_task_id)
                VALUES (1, 'src/b.py', 'style', 'low', 'not worth it', 'dismissed', NULL);
            INSERT INTO review_comments
                (review_id, file_path, category, severity, comment, resolution, deferred_task_id)
                VALUES (1, 'src/c.py', 'security', 'high', 'punted to follow-up', 'deferred', 42);
            -- NULL resolution (unresolved) should also be excluded.
            INSERT INTO review_comments
                (review_id, file_path, category, severity, comment)
                VALUES (1, 'src/d.py', 'perf', 'medium', 'still pending review');
        """)
        conn.commit()
        conn.row_factory = sqlite3.Row
        out = mod.fetch_deferred_review_comments(conn, 1)
        assert len(out) == 1
        row = out[0]
        assert row["category"] == "security"
        assert row["severity"] == "high"
        assert row["file_path"] == "src/c.py"
        assert row["deferred_task_id"] == 42
        assert row["sample"] == "punted to follow-up"

    def test_filters_to_task_and_truncates_sample(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1)
        long_body = "y" * 500  # proves sample is truncated, not raw-passed
        conn.executescript("""
            INSERT INTO tasks (id, summary) VALUES (2, 'other');
            INSERT INTO code_reviews (id, task_id) VALUES (1, 1);
            INSERT INTO code_reviews (id, task_id) VALUES (2, 2);
        """)
        conn.execute(
            "INSERT INTO review_comments "
            "(review_id, file_path, category, severity, comment, resolution, deferred_task_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "src/a.py", "correctness", "high", long_body, "deferred", 99),
        )
        conn.execute(
            "INSERT INTO review_comments "
            "(review_id, file_path, category, severity, comment, resolution, deferred_task_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (2, "src/z.py", "correctness", "high", "other task — ignored", "deferred", 77),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        out = mod.fetch_deferred_review_comments(conn, 1)
        assert len(out) == 1  # other-task row filtered out
        assert out[0]["deferred_task_id"] == 99
        assert len(out[0]["sample"]) <= mod.REVIEW_SAMPLE_MAX_CHARS
        assert out[0]["sample"].endswith("…")

    def test_allows_null_deferred_task_id(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1)
        conn.executescript("""
            INSERT INTO code_reviews (id, task_id) VALUES (1, 1);
            INSERT INTO review_comments
                (review_id, file_path, category, severity, comment, resolution, deferred_task_id)
                VALUES (1, 'src/a.py', 'style', 'low', 'deferred w/o linked task', 'deferred', NULL);
        """)
        conn.commit()
        conn.row_factory = sqlite3.Row
        out = mod.fetch_deferred_review_comments(conn, 1)
        assert len(out) == 1
        assert out[0]["deferred_task_id"] is None


# ── skipped_criteria ──────────────────────────────────────────────────


class TestSkippedCriteria:
    def test_empty_when_no_skip_notes(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1)
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, is_completed) VALUES (1, 'done normal', 1)"
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        assert mod.fetch_skipped_criteria(conn, 1) == []

    def test_returns_both_deferred_and_skip_verified(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1)
        conn.executescript("""
            INSERT INTO acceptance_criteria (task_id, criterion, is_completed, is_deferred, skip_note)
                VALUES (1, 'deferred one', 0, 1, 'punted to downstream task');
            INSERT INTO acceptance_criteria (task_id, criterion, is_completed, is_deferred, skip_note)
                VALUES (1, 'skip-verified one', 1, 0, 'no git diff — runtime only');
            INSERT INTO acceptance_criteria (task_id, criterion, is_completed, is_deferred, skip_note)
                VALUES (1, 'blank note ignored', 1, 0, '   ');
        """)
        conn.commit()
        conn.row_factory = sqlite3.Row
        out = mod.fetch_skipped_criteria(conn, 1)
        assert len(out) == 2
        assert {c["is_deferred"] for c in out} == {0, 1}
        assert all(c["skip_note"].strip() for c in out)


# ── tool_call_outliers ────────────────────────────────────────────────


class TestToolCallOutliers:
    def test_empty_when_no_sessions(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1, complexity="M")
        conn.close()
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        assert mod.fetch_tool_call_outliers(c, 1, "M") == []

    def test_threshold_scales_with_complexity(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1, complexity="XS")
        conn.executescript("""
            INSERT INTO task_sessions (id, task_id) VALUES (100, 1);
            INSERT INTO tool_call_stats (session_id, tool_name, call_count, total_cost)
                VALUES (100, 'Bash', 25, 0.50);
        """)
        conn.commit()
        conn.row_factory = sqlite3.Row
        # XS threshold is 20 → 25 calls crosses it.
        xs_out = mod.fetch_tool_call_outliers(conn, 1, "XS")
        assert len(xs_out) == 1
        assert xs_out[0]["tool_name"] == "Bash"
        assert xs_out[0]["call_count"] == 25
        assert xs_out[0]["threshold"] == 20
        # L threshold is 150 → same 25 calls does NOT cross it.
        l_out = mod.fetch_tool_call_outliers(conn, 1, "L")
        assert l_out == []

    def test_sums_across_sessions_filters_other_tasks(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1, complexity="M")
        conn.executescript("""
            INSERT INTO tasks (id, summary, complexity) VALUES (2, 'other', 'M');
            INSERT INTO task_sessions (id, task_id) VALUES (100, 1);
            INSERT INTO task_sessions (id, task_id) VALUES (101, 1);
            INSERT INTO task_sessions (id, task_id) VALUES (200, 2);
            INSERT INTO tool_call_stats (session_id, tool_name, call_count, total_cost)
                VALUES (100, 'Read', 50, 0.10);
            INSERT INTO tool_call_stats (session_id, tool_name, call_count, total_cost)
                VALUES (101, 'Read', 40, 0.08);
            INSERT INTO tool_call_stats (session_id, tool_name, call_count, total_cost)
                VALUES (200, 'Read', 999, 9.99);
        """)
        conn.commit()
        conn.row_factory = sqlite3.Row
        # M threshold=80, task 1 sums to 90 → included; task 2's 999 must not leak.
        out = mod.fetch_tool_call_outliers(conn, 1, "M")
        assert len(out) == 1
        assert out[0]["call_count"] == 90
        assert abs(out[0]["total_cost"] - 0.18) < 1e-9

    def test_null_complexity_uses_default_threshold(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1, complexity=None)
        conn.executescript("""
            INSERT INTO task_sessions (id, task_id) VALUES (100, 1);
            INSERT INTO tool_call_stats (session_id, tool_name, call_count, total_cost)
                VALUES (100, 'Grep', 90, 0.0);
        """)
        conn.commit()
        conn.row_factory = sqlite3.Row
        # None → default 80 → 90 crosses it.
        out = mod.fetch_tool_call_outliers(conn, 1, None)
        assert len(out) == 1
        assert out[0]["threshold"] == mod.CALL_COUNT_THRESHOLDS[None]
        assert out[0]["complexity"] is None


# ── unconsumed_next_steps ─────────────────────────────────────────────


class TestUnconsumedNextSteps:
    def test_empty_when_no_progress(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1)
        conn.close()
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        assert mod.fetch_unconsumed_next_steps(c, 1) == []

    def test_skips_null_and_blank_and_orders_chronologically(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1)
        conn.executescript("""
            INSERT INTO task_progress (task_id, next_steps, created_at)
                VALUES (1, 'first step',  '2025-01-01 00:00:00');
            INSERT INTO task_progress (task_id, next_steps, created_at)
                VALUES (1, NULL,           '2025-01-02 00:00:00');
            INSERT INTO task_progress (task_id, next_steps, created_at)
                VALUES (1, '   ',          '2025-01-03 00:00:00');
            INSERT INTO task_progress (task_id, next_steps, created_at)
                VALUES (1, 'second step', '2025-01-04 00:00:00');
        """)
        conn.commit()
        conn.row_factory = sqlite3.Row
        out = mod.fetch_unconsumed_next_steps(conn, 1)
        assert [r["next_steps"] for r in out] == ["first step", "second step"]
        assert out[0]["created_at"] < out[1]["created_at"]


# ── main: subprocess-level shape + errors ─────────────────────────────


class TestMainOutput:
    def test_always_emits_all_keys_even_when_empty(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=7)
        conn.close()
        rc, stdout, _ = _run_main(db_path, 7)
        assert rc == 0
        data = json.loads(stdout)
        assert set(data.keys()) == {
            "task_id", "complexity", "reopen_count", "rework_chain",
            "review_themes", "deferred_review_comments",
            "skipped_criteria", "tool_call_outliers",
            "unconsumed_next_steps",
        }
        # Empty-state shape: zero counts and empty arrays.
        assert data["reopen_count"] == 0
        assert data["rework_chain"] == {"fixes": [], "fixed_by": []}
        assert data["review_themes"] == []
        assert data["deferred_review_comments"] == []
        assert data["skipped_criteria"] == []
        assert data["tool_call_outliers"] == []
        assert data["unconsumed_next_steps"] == []

    def test_accepts_task_prefix_form(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=42)
        conn.close()
        rc, stdout, _ = _run_main(db_path, "TASK-42")
        assert rc == 0
        assert json.loads(stdout)["task_id"] == 42

    def test_not_found_exits_1(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1)
        conn.close()
        rc, _, stderr = _run_main(db_path, 9999)
        assert rc == 1
        assert "not found" in stderr

    def test_invalid_task_id_exits_1(self, tmp_path):
        db_path, conn = _make_db(tmp_path, task_id=1)
        conn.close()
        rc, _, stderr = _run_main(db_path, "not-a-number")
        assert rc == 2 or rc == 1  # argparse can exit 2 for type errors

    def test_direct_invocation_guard(self):
        result = subprocess.run(
            [sys.executable, os.path.join(BIN, "tusk-retro-signals.py")],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "tusk wrapper" in result.stderr or "retro-signals" in result.stderr
