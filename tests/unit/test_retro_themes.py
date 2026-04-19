"""Unit tests for tusk-retro-themes.py.

Covers:
- theme grouping by category (the "theme") on a seeded retro_findings fixture
- sort order: count DESC, then theme ASC (for deterministic rendering)
- window filtering via `--window-days` (rows older than the window are dropped;
  window_days=0 means "all history")
- min-recurrence HAVING filter (themes below the floor never leave SQL —
  satisfies TASK-108 criterion 480 that /retro consumes pre-aggregated
  tuples only)
- CLI flag validation (negative window_days, zero min_recurrence)
- empty-DB shape (keys always present, themes is []
- output is pre-aggregated tuples only — no raw-row escape hatch in the
  emitted JSON

The fixture schema here is a minimal subset of bin/tusk's real schema
(retro_findings + skill_runs + tasks only) — it's intentionally NOT meant
to mirror the canonical CREATE TABLE, since no schema-sync guard targets
retro_findings.
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
    "tusk_retro_themes",
    os.path.join(BIN, "tusk-retro-themes.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ── schema fixture ────────────────────────────────────────────────────
# Minimal subset — retro_findings + its FK parents. Not a canonical mirror;
# no schema-sync guard applies.

_SCHEMA = """
CREATE TABLE skill_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    started_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT
);
CREATE TABLE retro_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_run_id INTEGER NOT NULL,
    task_id INTEGER,
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    action_taken TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _make_db(tmp_path):
    db_path = str(tmp_path / "themes.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # Seed one skill_run so every finding has a valid FK.
    conn.execute("INSERT INTO skill_runs (id, skill_name) VALUES (1, 'retro')")
    conn.commit()
    return db_path, conn


def _seed_findings(conn, specs):
    """specs: iterable of (category, offset_days) — offset 0 means "now"."""
    for category, offset_days in specs:
        if offset_days == 0:
            conn.execute(
                "INSERT INTO retro_findings (skill_run_id, category, summary) "
                "VALUES (1, ?, 's')",
                (category,),
            )
        else:
            conn.execute(
                "INSERT INTO retro_findings "
                "(skill_run_id, category, summary, created_at) "
                "VALUES (1, ?, 's', datetime('now', ?))",
                (category, f"-{offset_days} days"),
            )
    conn.commit()


def _run_main(db_path, *cli_args, config_path="fake.json"):
    result = subprocess.run(
        [sys.executable, os.path.join(BIN, "tusk-retro-themes.py"),
         db_path, config_path, *cli_args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode, result.stdout, result.stderr


class TestFetchThemes:
    """Direct unit tests against fetch_themes() — no subprocess overhead."""

    def test_groups_by_category_and_counts(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _seed_findings(conn, [("A", 0), ("A", 1), ("B", 2), ("A", 3), ("C", 4)])

        result = mod.fetch_themes(conn, window_days=30, min_recurrence=1)

        assert result["total_findings"] == 5
        assert {t["theme"]: t["count"] for t in result["themes"]} == {
            "A": 3,
            "B": 1,
            "C": 1,
        }

    def test_sort_order_count_desc_then_theme_asc(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        # Counts: E=3, A=3, B=2, D=1. Tie between A and E → alpha order.
        _seed_findings(conn, [
            ("E", 0), ("E", 1), ("E", 2),
            ("A", 0), ("A", 1), ("A", 2),
            ("B", 0), ("B", 1),
            ("D", 0),
        ])

        result = mod.fetch_themes(conn, window_days=30, min_recurrence=1)

        themes = [(t["theme"], t["count"]) for t in result["themes"]]
        assert themes == [("A", 3), ("E", 3), ("B", 2), ("D", 1)]

    def test_window_filter_drops_old_rows(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        # 2 recent A, 1 old A (45 days), 1 recent B.
        _seed_findings(conn, [("A", 0), ("A", 5), ("A", 45), ("B", 2)])

        result = mod.fetch_themes(conn, window_days=30, min_recurrence=1)

        assert result["total_findings"] == 3
        assert {t["theme"]: t["count"] for t in result["themes"]} == {"A": 2, "B": 1}

    def test_window_zero_means_all_history(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _seed_findings(conn, [("A", 0), ("A", 400), ("B", 1000)])

        result = mod.fetch_themes(conn, window_days=0, min_recurrence=1)

        assert result["total_findings"] == 3
        assert {t["theme"]: t["count"] for t in result["themes"]} == {"A": 2, "B": 1}

    def test_min_recurrence_filters_in_sql(self, tmp_path):
        """Themes below the recurrence floor must never leave SQL — /retro
        consumes only pre-aggregated tuples (TASK-108 criterion 480)."""
        db_path, conn = _make_db(tmp_path)
        _seed_findings(conn, [
            ("A", 0), ("A", 1), ("A", 2),   # 3x
            ("B", 0), ("B", 1),              # 2x
            ("C", 0),                        # 1x
        ])

        result = mod.fetch_themes(conn, window_days=30, min_recurrence=3)

        themes = [t["theme"] for t in result["themes"]]
        assert themes == ["A"]
        # total_findings is the raw count pre-HAVING; it is intentionally
        # NOT affected by the min_recurrence filter so callers can tell
        # "6 findings, only 1 recurring theme" at a glance.
        assert result["total_findings"] == 6

    def test_empty_db_returns_empty_themes_with_shape(self, tmp_path):
        db_path, conn = _make_db(tmp_path)

        result = mod.fetch_themes(conn, window_days=30, min_recurrence=1)

        assert result == {
            "window_days": 30,
            "min_recurrence": 1,
            "total_findings": 0,
            "themes": [],
        }


class TestMainCLI:
    """Subprocess tests for flag handling and JSON output shape."""

    def test_default_flags_emit_all_themes_in_30_day_window(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _seed_findings(conn, [("A", 0), ("A", 1), ("B", 0)])
        conn.close()

        returncode, stdout, stderr = _run_main(db_path)

        assert returncode == 0, stderr
        data = json.loads(stdout)
        assert data["window_days"] == mod.DEFAULT_WINDOW_DAYS
        assert data["min_recurrence"] == mod.DEFAULT_MIN_RECURRENCE
        assert data["themes"] == [{"theme": "A", "count": 2}, {"theme": "B", "count": 1}]

    def test_custom_window_and_min_recurrence_propagate(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _seed_findings(conn, [
            ("A", 0), ("A", 1), ("A", 2),
            ("B", 0),
        ])
        conn.close()

        returncode, stdout, _ = _run_main(
            db_path, "--window-days", "7", "--min-recurrence", "3"
        )

        assert returncode == 0
        data = json.loads(stdout)
        assert data["window_days"] == 7
        assert data["min_recurrence"] == 3
        assert data["themes"] == [{"theme": "A", "count": 3}]

    def test_negative_window_days_is_rejected(self, tmp_path):
        db_path, _ = _make_db(tmp_path)

        returncode, _, stderr = _run_main(db_path, "--window-days", "-1")

        assert returncode == 1
        assert "window-days" in stderr

    def test_zero_min_recurrence_is_rejected(self, tmp_path):
        db_path, _ = _make_db(tmp_path)

        returncode, _, stderr = _run_main(db_path, "--min-recurrence", "0")

        assert returncode == 1
        assert "min-recurrence" in stderr

    def test_output_is_pre_aggregated_tuples_only(self, tmp_path):
        """No raw-row escape hatch in the JSON — only {theme, count} per row."""
        db_path, conn = _make_db(tmp_path)
        _seed_findings(conn, [("A", 0), ("A", 1), ("B", 0)])
        conn.close()

        returncode, stdout, _ = _run_main(db_path)

        assert returncode == 0
        data = json.loads(stdout)
        for entry in data["themes"]:
            assert set(entry.keys()) == {"theme", "count"}, (
                "retro-themes must emit only pre-aggregated (theme, count) "
                "tuples — raw retro_findings fields must never leak to the "
                "caller per TASK-108 criterion 480"
            )
