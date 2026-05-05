"""Unit tests for tusk-retro-themes.py.

Covers:
- theme grouping by content-derived terms extracted from retro_findings.summary
  (issue #551 — prior implementation grouped by `category` and produced
  tautological recurrence counts per A/B/C/D/E bucket)
- single-letter category codes never appear in the emitted themes
- bigrams compose from consecutive non-stop content words
- recurrence is per-finding (set semantics) — a term repeated within one
  summary still counts once for that row
- sort order: count DESC, then theme ASC (for deterministic rendering)
- window filtering via `--window-days` (rows older than the window are dropped;
  window_days=0 means "all history")
- min-recurrence filter (themes below the floor are dropped before emission —
  satisfies TASK-108 criterion 480 that /retro consumes pre-aggregated
  tuples only)
- CLI flag validation (negative window_days, zero min_recurrence)
- empty-DB shape (keys always present, themes is [])
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
import re
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
    """specs: iterable of (summary, offset_days) — offset 0 means "now".

    `category` is set to a constant "A" (NOT NULL in the schema but
    irrelevant to content-derived aggregation per issue #551)."""
    for summary, offset_days in specs:
        if offset_days == 0:
            conn.execute(
                "INSERT INTO retro_findings (skill_run_id, category, summary) "
                "VALUES (1, 'A', ?)",
                (summary,),
            )
        else:
            conn.execute(
                "INSERT INTO retro_findings "
                "(skill_run_id, category, summary, created_at) "
                "VALUES (1, 'A', ?, datetime('now', ?))",
                (summary, f"-{offset_days} days"),
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


class TestExtractTerms:
    """Direct unit tests for the term-extraction helper."""

    def test_lowercases_and_strips_punctuation(self):
        terms = mod._extract_terms("MANIFEST drift, in branch sync.")
        assert "manifest" in terms
        assert "drift" in terms
        assert "branch" in terms
        # Stop word "in" must not appear.
        assert "in" not in terms

    def test_drops_short_tokens(self):
        # "a" and "of" are stop words; "is" is short and a stop word; "x"
        # is too short on its own. Only "long" and "word" should survive.
        terms = mod._extract_terms("a x is of long word")
        assert "long" in terms
        assert "word" in terms
        assert "x" not in terms

    def test_emits_bigrams_of_consecutive_content_tokens(self):
        terms = mod._extract_terms("phantom MANIFEST extras after install")
        # Stop-word "after" splits the bigram chain.
        assert "phantom manifest" in terms
        assert "manifest extras" in terms
        # "extras after" must NOT appear because "after" is a stop word.
        assert "extras after" not in terms
        # "after install" likewise must not appear.
        assert "after install" not in terms
        # "extras install" must NOT appear because the bigram is built
        # from consecutive surviving tokens — "extras" and "install" are
        # adjacent in the post-filter list, so this DOES appear.
        assert "extras install" in terms

    def test_repeated_term_within_one_summary_is_set_deduped(self):
        # Set semantics — a repeated word in one summary contributes the
        # unigram once and the same-word bigram once, regardless of how many
        # times it repeats. The point of set semantics is that downstream
        # recurrence counts how many findings the term appears in, not how
        # often it repeats within one finding.
        terms = mod._extract_terms("manifest manifest manifest")
        assert terms == {"manifest", "manifest manifest"}

    def test_empty_or_none_summary_returns_empty_set(self):
        assert mod._extract_terms("") == set()
        assert mod._extract_terms(None) == set()


class TestFetchThemes:
    """Direct unit tests against fetch_themes() — no subprocess overhead."""

    def test_groups_by_content_derived_terms(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _seed_findings(conn, [
            ("MANIFEST drift in skills sync", 0),
            ("phantom MANIFEST extras after install", 1),
            ("MANIFEST entries diverged from disk", 2),
            ("Branch protection blocked the push", 3),
        ])

        result = mod.fetch_themes(conn, window_days=30, min_recurrence=3)

        # "manifest" appears in 3 distinct findings — survives the floor.
        themes = {t["theme"]: t["count"] for t in result["themes"]}
        assert themes.get("manifest") == 3
        # total_findings reflects the row count, not term count.
        assert result["total_findings"] == 4

    def test_no_single_letter_category_codes_in_output(self, tmp_path):
        """Issue #551: A/B/C/D/E must NOT appear as themes — short
        single-character tokens are filtered by length, and the category
        column is not consulted for grouping."""
        db_path, conn = _make_db(tmp_path)
        _seed_findings(conn, [
            ("real recurring theme about manifest issues", 0),
            ("another finding mentioning manifest drift", 1),
            ("yet another manifest related observation", 2),
        ])

        result = mod.fetch_themes(conn, window_days=30, min_recurrence=1)

        for entry in result["themes"]:
            assert not re.match(r"^[A-Ea-e]$", entry["theme"]), (
                f"single-letter category code leaked into themes: "
                f"{entry['theme']!r}"
            )

    def test_sort_order_count_desc_then_theme_asc(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        # "alpha" appears in 3 rows, "bravo" in 3 rows, "charlie" in 2.
        # Tie between alpha and bravo → alpha order wins.
        _seed_findings(conn, [
            ("alpha bravo charlie", 0),
            ("alpha bravo charlie", 1),
            ("alpha bravo", 2),
        ])

        result = mod.fetch_themes(conn, window_days=30, min_recurrence=2)

        # Counts: alpha=3, bravo=3, charlie=2 (plus their bigrams). The
        # unigrams must be ordered alpha < bravo < charlie at the same count.
        unigrams = [t for t in result["themes"] if " " not in t["theme"]]
        assert unigrams[0] == {"theme": "alpha", "count": 3}
        assert unigrams[1] == {"theme": "bravo", "count": 3}
        assert unigrams[2] == {"theme": "charlie", "count": 2}

    def test_window_filter_drops_old_rows(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        # 2 recent "alpha", 1 old "alpha" (45 days), 1 recent "bravo".
        _seed_findings(conn, [
            ("alpha first", 0),
            ("alpha second", 5),
            ("alpha old", 45),
            ("bravo recent", 2),
        ])

        result = mod.fetch_themes(conn, window_days=30, min_recurrence=1)

        themes = {t["theme"]: t["count"] for t in result["themes"]}
        assert result["total_findings"] == 3
        assert themes.get("alpha") == 2
        assert themes.get("bravo") == 1

    def test_window_zero_means_all_history(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _seed_findings(conn, [
            ("alpha first", 0),
            ("alpha ancient", 400),
            ("bravo prehistoric", 1000),
        ])

        result = mod.fetch_themes(conn, window_days=0, min_recurrence=1)

        themes = {t["theme"]: t["count"] for t in result["themes"]}
        assert result["total_findings"] == 3
        assert themes.get("alpha") == 2
        assert themes.get("bravo") == 1

    def test_min_recurrence_filters_below_floor(self, tmp_path):
        """Themes below the recurrence floor are dropped before emission so
        /retro consumes only pre-aggregated tuples (TASK-108 criterion 480)."""
        db_path, conn = _make_db(tmp_path)
        _seed_findings(conn, [
            ("alpha", 0), ("alpha", 1), ("alpha", 2),    # 3x
            ("bravo", 0), ("bravo", 1),                   # 2x
            ("charlie", 0),                               # 1x
        ])

        result = mod.fetch_themes(conn, window_days=30, min_recurrence=3)

        themes = {t["theme"] for t in result["themes"]}
        assert "alpha" in themes
        assert "bravo" not in themes
        assert "charlie" not in themes
        # total_findings is the raw count pre-recurrence-filter; it is
        # intentionally NOT affected by min_recurrence so callers can tell
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

    def test_default_flags_emit_themes_in_30_day_window(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _seed_findings(conn, [
            ("manifest drift again", 0),
            ("manifest drift returns", 1),
            ("unrelated note", 0),
        ])
        conn.close()

        returncode, stdout, stderr = _run_main(db_path)

        assert returncode == 0, stderr
        data = json.loads(stdout)
        assert data["window_days"] == mod.DEFAULT_WINDOW_DAYS
        assert data["min_recurrence"] == mod.DEFAULT_MIN_RECURRENCE
        themes = {t["theme"]: t["count"] for t in data["themes"]}
        # Both "manifest" and "drift" appear in 2 rows; "manifest drift"
        # bigram likewise. The unrelated row contributes its own terms at
        # count=1.
        assert themes.get("manifest") == 2
        assert themes.get("drift") == 2
        assert themes.get("manifest drift") == 2

    def test_custom_window_and_min_recurrence_propagate(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _seed_findings(conn, [
            ("repeated phrase one", 0),
            ("repeated phrase two", 1),
            ("repeated phrase three", 2),
            ("unrelated tail", 0),
        ])
        conn.close()

        returncode, stdout, _ = _run_main(
            db_path, "--window-days", "7", "--min-recurrence", "3"
        )

        assert returncode == 0
        data = json.loads(stdout)
        assert data["window_days"] == 7
        assert data["min_recurrence"] == 3
        themes = {t["theme"]: t["count"] for t in data["themes"]}
        # "repeated" and "phrase" each appear in 3 rows; "repeated phrase"
        # bigram likewise.
        assert themes.get("repeated") == 3
        assert themes.get("phrase") == 3
        assert themes.get("repeated phrase") == 3

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
        _seed_findings(conn, [
            ("alpha bravo charlie", 0),
            ("alpha bravo delta", 1),
        ])
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
