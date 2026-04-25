"""Unit tests for tokens_per_user_msg in tusk-skill-run list output.

Covers:
- _tokens_per_user_msg returns None when count is 0 / NULL
- _tokens_per_user_msg returns integer division otherwise
- cmd_list prints '-' for legacy rows with NULL counts
- cmd_list prints integer T/Msg for rows with populated columns
"""

import importlib.util
import io
import os
import sqlite3
import sys
from contextlib import redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

# Loader path must include BIN so the script's `import tusk_loader` finds the module.
sys.path.insert(0, BIN)
_spec = importlib.util.spec_from_file_location(
    "tusk_skill_run",
    os.path.join(BIN, "tusk-skill-run.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


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


class TestTokensPerUserMsg:

    def test_zero_count_returns_none(self):
        assert mod._tokens_per_user_msg(100, 0) is None

    def test_null_count_returns_none(self):
        assert mod._tokens_per_user_msg(100, None) is None

    def test_null_tokens_returns_none(self):
        assert mod._tokens_per_user_msg(None, 5) is None

    def test_integer_division(self):
        assert mod._tokens_per_user_msg(100, 4) == 25
        assert mod._tokens_per_user_msg(101, 4) == 25  # floor division


class TestCmdListOutput:

    def _run_list(self, conn):
        buf = io.StringIO()
        with redirect_stdout(buf):
            mod.cmd_list(conn, None, 50)
        return buf.getvalue()

    def test_header_includes_t_msg(self, conn):
        conn.execute(
            "INSERT INTO skill_runs (skill_name, ended_at, cost_dollars, tokens_in, model, "
            "user_prompt_tokens, user_prompt_count) VALUES ('tusk', datetime('now'), 0.05, 1000, "
            "'claude-opus-4-7', 200, 4)"
        )
        conn.commit()
        out = self._run_list(conn)
        assert "T/Msg" in out
        # Avg = 200 // 4 = 50, formatted with comma still "50"
        assert "50" in out

    def test_legacy_row_with_null_columns_prints_dash(self, conn):
        conn.execute(
            "INSERT INTO skill_runs (skill_name, ended_at, cost_dollars, tokens_in, model) "
            "VALUES ('tusk', datetime('now'), 0.05, 1000, 'claude-opus-4-7')"
        )
        conn.commit()
        out = self._run_list(conn)
        # Pull the data row (last non-empty line), ensure T/Msg slot is "-"
        data_lines = [l for l in out.strip().split("\n") if l and not l.startswith("-") and "T/Msg" not in l]
        assert any("-" in l for l in data_lines)

    def test_zero_count_prints_dash_not_division_error(self, conn):
        conn.execute(
            "INSERT INTO skill_runs (skill_name, ended_at, cost_dollars, tokens_in, model, "
            "user_prompt_tokens, user_prompt_count) VALUES ('tusk', datetime('now'), 0.05, 1000, "
            "'claude-opus-4-7', 0, 0)"
        )
        conn.commit()
        # Must not raise ZeroDivisionError
        out = self._run_list(conn)
        assert "T/Msg" in out
