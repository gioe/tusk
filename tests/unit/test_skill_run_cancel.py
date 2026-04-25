"""Unit tests for `tusk skill-run cancel` (cmd_cancel in bin/tusk-skill-run.py).

Loads tusk-skill-run.py via importlib (hyphenated filename) and exercises the
cancel subcommand against an in-memory SQLite connection so the acceptance
criterion 'cancel removes or closes the specified open row' has regression
coverage.

Covers:
  - Happy path: an open row is closed with zero cost/tokens, null metadata.
  - Already-finished row: warns, leaves real cost/metadata untouched, exits 0.
  - Missing run_id: warns, exits 0 (cancel is cleanup-only — missing rows are
    equivalent to already-cleaned).
  - Non-integer run_id surfaced via the main() dispatcher: exits 1.
"""

import importlib.util
import io
import os
import sqlite3
import sys

import pytest
from contextlib import redirect_stderr, redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_skill_run():
    bin_dir = os.path.join(REPO_ROOT, "bin")
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    path = os.path.join(bin_dir, "tusk-skill-run.py")
    spec = importlib.util.spec_from_file_location("tusk_skill_run_under_test", path)
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
    model TEXT,
    metadata TEXT,
    request_count INTEGER,
    task_id INTEGER,
    user_prompt_tokens INTEGER,
    user_prompt_count INTEGER
);
"""


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SKILL_RUNS_TABLE)
    yield c
    c.close()


def _fetch(conn, run_id):
    return conn.execute(
        "SELECT ended_at, cost_dollars, tokens_in, tokens_out, model, metadata"
        " FROM skill_runs WHERE id = ?",
        (run_id,),
    ).fetchone()


class TestCmdCancel:
    def test_open_row_is_closed_with_zero_cost(self, conn):
        cur = conn.execute(
            "INSERT INTO skill_runs (skill_name) VALUES ('test-skill')"
        )
        conn.commit()
        run_id = cur.lastrowid

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            skill_run.cmd_cancel(conn, run_id)

        row = _fetch(conn, run_id)
        assert row["ended_at"] is not None, "cancel must set ended_at on open row"
        assert row["cost_dollars"] == 0
        assert row["tokens_in"] == 0
        assert row["tokens_out"] == 0
        assert row["model"] == ""
        assert row["metadata"] is None
        assert "cancelled" in out.getvalue()

    def test_already_finished_row_is_not_overwritten(self, conn):
        cur = conn.execute(
            "INSERT INTO skill_runs"
            " (skill_name, ended_at, cost_dollars, tokens_in, tokens_out, model, metadata)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("test-skill", "2026-04-18 10:00:00", 1.2345, 9999, 8888, "claude-opus-4", '{"x":1}'),
        )
        conn.commit()
        run_id = cur.lastrowid

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            skill_run.cmd_cancel(conn, run_id)

        row = _fetch(conn, run_id)
        assert row["ended_at"] == "2026-04-18 10:00:00"
        assert row["cost_dollars"] == 1.2345
        assert row["tokens_in"] == 9999
        assert row["model"] == "claude-opus-4"
        assert row["metadata"] == '{"x":1}'
        assert "already finished" in err.getvalue()

    def test_missing_run_id_warns_and_returns_zero(self, conn):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            skill_run.cmd_cancel(conn, 99999)

        assert "No skill run found" in err.getvalue()
        count = conn.execute("SELECT COUNT(*) FROM skill_runs").fetchone()[0]
        assert count == 0


class TestCancelDispatcher:
    def test_non_integer_run_id_exits_one(self, tmp_path, monkeypatch):
        db_path = tmp_path / "tasks.db"
        c = sqlite3.connect(str(db_path))
        c.executescript(_SKILL_RUNS_TABLE)
        c.commit()
        c.close()

        monkeypatch.setattr(sys, "argv", ["tusk-skill-run", str(db_path), "", "cancel", "not-a-number"])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), pytest.raises(SystemExit) as exc_info:
            skill_run.main()

        assert exc_info.value.code == 1
        assert "run_id must be an integer" in err.getvalue()

    def test_cancel_with_no_run_id_exits_one(self, tmp_path, monkeypatch):
        db_path = tmp_path / "tasks.db"
        c = sqlite3.connect(str(db_path))
        c.executescript(_SKILL_RUNS_TABLE)
        c.commit()
        c.close()

        monkeypatch.setattr(sys, "argv", ["tusk-skill-run", str(db_path), "", "cancel"])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), pytest.raises(SystemExit) as exc_info:
            skill_run.main()

        assert exc_info.value.code == 1
        assert "Usage: tusk skill-run cancel" in err.getvalue()


# ---------------------------------------------------------------------------
# Schema sync guard: _SKILL_RUNS_TABLE fixture vs bin/tusk CREATE TABLE skill_runs
# ---------------------------------------------------------------------------

def _extract_table_columns(sql_text, table_name):
    """Return the set of column names defined in the CREATE TABLE <table_name> block.

    Mirrors the helper of the same name in tests/unit/test_dashboard_data.py;
    duplicated so each test file's fixture-vs-bin/tusk guard is self-contained.
    """
    import re

    header = re.search(rf"CREATE TABLE {re.escape(table_name)}\s*\(", sql_text, re.IGNORECASE)
    if not header:
        return set()

    body_start = sql_text.index("(", header.start())
    body_lines = []
    for line in sql_text[body_start + 1:].splitlines():
        if line.strip().startswith(")"):
            break
        body_lines.append(line)

    columns = set()
    for line in body_lines:
        line = line.strip().rstrip(",")
        if not line:
            continue
        if re.match(r"(FOREIGN KEY|PRIMARY KEY|UNIQUE|CHECK|CONSTRAINT)\b", line, re.IGNORECASE):
            continue
        col_match = re.match(r"(\w+)", line)
        if col_match:
            columns.add(col_match.group(1).lower())
    return columns


class TestSkillRunsSchemaSync:
    """Guard against drift between _SKILL_RUNS_TABLE fixture and bin/tusk CREATE TABLE skill_runs."""

    def test_fixture_matches_bin_tusk(self):
        tusk_path = os.path.join(REPO_ROOT, "bin", "tusk")
        with open(tusk_path) as f:
            tusk_sql = f.read()

        tusk_cols = _extract_table_columns(tusk_sql, "skill_runs")
        fixture_cols = _extract_table_columns(_SKILL_RUNS_TABLE, "skill_runs")

        assert tusk_cols, "Could not find CREATE TABLE skill_runs in bin/tusk"
        assert fixture_cols, "Could not find CREATE TABLE skill_runs in _SKILL_RUNS_TABLE fixture"

        missing_from_fixture = tusk_cols - fixture_cols
        extra_in_fixture = fixture_cols - tusk_cols

        assert not missing_from_fixture, (
            f"skill_runs columns in bin/tusk missing from _SKILL_RUNS_TABLE fixture: {sorted(missing_from_fixture)}. "
            "Update _SKILL_RUNS_TABLE in tests/unit/test_skill_run_cancel.py to match."
        )
        assert not extra_in_fixture, (
            f"skill_runs columns in _SKILL_RUNS_TABLE fixture not in bin/tusk: {sorted(extra_in_fixture)}. "
            "Update _SKILL_RUNS_TABLE in tests/unit/test_skill_run_cancel.py to match."
        )
