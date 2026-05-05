"""Unit tests for tusk-retro-patches.py.

Covers:
- default --window-days filter (30) drops rows older than the window
- --window-days 0 disables the date filter
- --window-days negative is rejected with exit 1
- --unconfirmed excludes patches with a later skill-patch-confirmed:<file> row
  matching the same target file
- a skill-patch-confirmed row created BEFORE the patch must NOT mark it
  confirmed (chronological order matters)
- a skill-patch-confirmed row for a different file must NOT mark this patch
  confirmed (target file match required)
- non-skill-patch action_taken values (e.g., lint-rule, convention) are
  excluded from the result
- empty DB (or all rows outside the window) returns []
- JSON shape is stable: every row has exactly the documented keys
- newest-first sort order is honored

The fixture schema is a minimal subset of bin/tusk's real schema —
retro_findings + its FK parents only. No schema-sync guard targets
retro_findings, so this fixture intentionally does not mirror the
canonical CREATE TABLE.
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_retro_patches",
    os.path.join(BIN, "tusk-retro-patches.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


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
    db_path = str(tmp_path / "patches.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO skill_runs (id, skill_name) VALUES (1, 'retro')")
    conn.commit()
    return db_path, conn


def _insert(conn, action_taken, *, offset_seconds=0, summary="finding"):
    """Insert a retro_findings row with action_taken and a relative offset.

    offset_seconds shifts created_at backwards from now by N seconds —
    coarse enough to order rows deterministically without juggling days
    when the test only cares about chronological ordering.
    """
    if offset_seconds == 0:
        conn.execute(
            "INSERT INTO retro_findings "
            "(skill_run_id, category, summary, action_taken) "
            "VALUES (1, 'A', ?, ?)",
            (summary, action_taken),
        )
    else:
        conn.execute(
            "INSERT INTO retro_findings "
            "(skill_run_id, category, summary, action_taken, created_at) "
            "VALUES (1, 'A', ?, ?, datetime('now', ?))",
            (summary, action_taken, f"-{offset_seconds} seconds"),
        )
    conn.commit()


def _insert_old(conn, action_taken, *, offset_days):
    """Insert a row with created_at offset by N days (for window-days tests)."""
    conn.execute(
        "INSERT INTO retro_findings "
        "(skill_run_id, category, summary, action_taken, created_at) "
        "VALUES (1, 'A', ?, ?, datetime('now', ?))",
        ("old finding", action_taken, f"-{offset_days} days"),
    )
    conn.commit()


def _run_main(db_path, *cli_args, config_path="fake.json"):
    result = subprocess.run(
        [sys.executable, os.path.join(BIN, "tusk-retro-patches.py"),
         db_path, config_path, *cli_args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode, result.stdout, result.stderr


class TestFetchPatches:
    def test_default_window_includes_recent_rows(self, tmp_path):
        _, conn = _make_db(tmp_path)
        _insert(conn, "skill-patch:skills/retro/SKILL.md")

        rows = mod.fetch_patches(conn, window_days=30, unconfirmed_only=False)

        assert len(rows) == 1
        assert rows[0]["target_file"] == "skills/retro/SKILL.md"

    def test_window_drops_rows_older_than_n_days(self, tmp_path):
        _, conn = _make_db(tmp_path)
        _insert(conn, "skill-patch:CLAUDE.md")
        _insert_old(conn, "skill-patch:AGENTS.md", offset_days=45)

        rows = mod.fetch_patches(conn, window_days=30, unconfirmed_only=False)

        assert len(rows) == 1
        assert rows[0]["target_file"] == "CLAUDE.md"

    def test_window_zero_means_all_history(self, tmp_path):
        _, conn = _make_db(tmp_path)
        _insert(conn, "skill-patch:CLAUDE.md")
        _insert_old(conn, "skill-patch:AGENTS.md", offset_days=400)

        rows = mod.fetch_patches(conn, window_days=0, unconfirmed_only=False)

        files = {r["target_file"] for r in rows}
        assert files == {"CLAUDE.md", "AGENTS.md"}

    def test_unconfirmed_excludes_patches_with_later_confirmed_row(self, tmp_path):
        _, conn = _make_db(tmp_path)
        _insert(conn, "skill-patch:CLAUDE.md", offset_seconds=120)
        _insert(conn, "skill-patch-confirmed:CLAUDE.md", offset_seconds=10)
        _insert(conn, "skill-patch:AGENTS.md", offset_seconds=60)

        rows = mod.fetch_patches(conn, window_days=30, unconfirmed_only=True)

        files = {r["target_file"] for r in rows}
        # CLAUDE.md is filtered (confirmed); AGENTS.md remains.
        assert files == {"AGENTS.md"}

    def test_unconfirmed_ignores_confirmed_rows_dated_before_the_patch(self, tmp_path):
        """A confirmation row whose created_at is older than the patch must
        NOT mark the patch confirmed — the confirmation only counts if it
        observed the patch's effect, which requires happening AFTER it."""
        _, conn = _make_db(tmp_path)
        _insert(conn, "skill-patch-confirmed:CLAUDE.md", offset_seconds=120)
        _insert(conn, "skill-patch:CLAUDE.md", offset_seconds=10)

        rows = mod.fetch_patches(conn, window_days=30, unconfirmed_only=True)

        assert len(rows) == 1
        assert rows[0]["target_file"] == "CLAUDE.md"

    def test_unconfirmed_requires_target_file_match(self, tmp_path):
        """A confirmation for a DIFFERENT file must not mark this patch
        confirmed — the file in the action_taken suffix must match."""
        _, conn = _make_db(tmp_path)
        _insert(conn, "skill-patch:CLAUDE.md", offset_seconds=120)
        _insert(conn, "skill-patch-confirmed:AGENTS.md", offset_seconds=10)

        rows = mod.fetch_patches(conn, window_days=30, unconfirmed_only=True)

        assert len(rows) == 1
        assert rows[0]["target_file"] == "CLAUDE.md"

    def test_non_skill_patch_action_taken_values_are_excluded(self, tmp_path):
        _, conn = _make_db(tmp_path)
        _insert(conn, "skill-patch:CLAUDE.md")
        _insert(conn, "lint-rule:my-rule")
        _insert(conn, "convention:my-convention")
        _insert(conn, "task-created:#42")
        _insert(conn, None)

        rows = mod.fetch_patches(conn, window_days=30, unconfirmed_only=False)

        assert len(rows) == 1
        assert rows[0]["target_file"] == "CLAUDE.md"

    def test_empty_db_returns_empty_list(self, tmp_path):
        _, conn = _make_db(tmp_path)

        rows = mod.fetch_patches(conn, window_days=30, unconfirmed_only=False)

        assert rows == []

    def test_newest_first_sort_order(self, tmp_path):
        _, conn = _make_db(tmp_path)
        _insert(conn, "skill-patch:third", offset_seconds=300)
        _insert(conn, "skill-patch:first", offset_seconds=10)
        _insert(conn, "skill-patch:second", offset_seconds=120)

        rows = mod.fetch_patches(conn, window_days=30, unconfirmed_only=False)

        assert [r["target_file"] for r in rows] == ["first", "second", "third"]


class TestMainCLI:
    def test_default_invocation_emits_json_array(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _insert(conn, "skill-patch:skills/retro/SKILL.md")
        conn.close()

        returncode, stdout, stderr = _run_main(db_path)

        assert returncode == 0, stderr
        data = json.loads(stdout)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["target_file"] == "skills/retro/SKILL.md"

    def test_json_shape_is_stable(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _insert(conn, "skill-patch:CLAUDE.md")
        conn.close()

        returncode, stdout, _ = _run_main(db_path)

        assert returncode == 0
        data = json.loads(stdout)
        assert len(data) == 1
        assert set(data[0].keys()) == {
            "finding_id",
            "skill_run_id",
            "task_id",
            "action_taken",
            "target_file",
            "created_at",
            "age_days",
        }

    def test_empty_db_returns_empty_array(self, tmp_path):
        db_path, _ = _make_db(tmp_path)

        returncode, stdout, _ = _run_main(db_path)

        assert returncode == 0
        assert json.loads(stdout) == []

    def test_unconfirmed_flag_filters_confirmed_patches(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _insert(conn, "skill-patch:CLAUDE.md", offset_seconds=120)
        _insert(conn, "skill-patch-confirmed:CLAUDE.md", offset_seconds=10)
        conn.close()

        # Without --unconfirmed: row is present.
        returncode, stdout, _ = _run_main(db_path)
        assert returncode == 0
        assert any(
            r["target_file"] == "CLAUDE.md" for r in json.loads(stdout)
        )

        # With --unconfirmed: row is filtered out.
        returncode, stdout, _ = _run_main(db_path, "--unconfirmed")
        assert returncode == 0
        assert json.loads(stdout) == []

    def test_negative_window_days_is_rejected(self, tmp_path):
        db_path, _ = _make_db(tmp_path)

        returncode, _, stderr = _run_main(db_path, "--window-days", "-1")

        assert returncode == 1
        assert "window-days" in stderr
