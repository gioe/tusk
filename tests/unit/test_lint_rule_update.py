"""Unit tests for `tusk lint-rule update` (cmd_update in tusk-lint-rules.py).

Verifies:
- each updatable column (file_glob, grep_pattern, message, is_blocking,
  source_skill) can be edited individually without touching the others
- rule id and created_at are preserved across updates
- missing id exits 2 with a stderr error (mirrors cmd_remove)
- omitting all flags exits 2 with a stderr error (no implicit no-op UPDATE)
"""

import importlib.util
import os
import sqlite3

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_lint_rules",
    os.path.join(BIN, "tusk-lint-rules.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


_RULE_SCHEMA = """
CREATE TABLE lint_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grep_pattern TEXT NOT NULL,
    file_glob TEXT NOT NULL,
    message TEXT NOT NULL,
    is_blocking INTEGER NOT NULL DEFAULT 0,
    source_skill TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    CHECK (is_blocking IN (0, 1))
);
"""


def _make_db(tmp_path) -> str:
    """Create a tmp DB seeded with one lint_rule row and return its path."""
    db_path = str(tmp_path / "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(_RULE_SCHEMA)
    conn.execute(
        "INSERT INTO lint_rules (id, grep_pattern, file_glob, message,"
        " is_blocking, source_skill, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (5, "old_pattern", "old_glob", "old_message", 0, "old_skill",
         "2026-01-01 00:00:00"),
    )
    conn.commit()
    conn.close()
    return db_path


def _row(db_path: str, rule_id: int):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM lint_rules WHERE id = ?", (rule_id,)
        ).fetchone()
    finally:
        conn.close()


def _run(db_path: str, *update_args) -> int:
    # argv shape mirrors the tusk wrapper: [script, db, config, subcommand, ...]
    argv = ["tusk-lint-rules.py", db_path, "/dev/null", "update", *update_args]
    return mod.main(argv)


class TestSelectiveUpdate:
    def test_file_glob_only(self, tmp_path, capsys):
        db = _make_db(tmp_path)
        exit_code = _run(db, "5", "--file-glob", "new/**/*.py")
        assert exit_code == 0
        assert capsys.readouterr().out.strip() == "5"
        row = _row(db, 5)
        assert row["file_glob"] == "new/**/*.py"
        # other fields unchanged
        assert row["grep_pattern"] == "old_pattern"
        assert row["message"] == "old_message"
        assert row["is_blocking"] == 0
        assert row["source_skill"] == "old_skill"

    def test_grep_pattern_only(self, tmp_path):
        db = _make_db(tmp_path)
        assert _run(db, "5", "--grep-pattern", r"new\s+pattern") == 0
        row = _row(db, 5)
        assert row["grep_pattern"] == r"new\s+pattern"
        assert row["file_glob"] == "old_glob"
        assert row["message"] == "old_message"
        assert row["is_blocking"] == 0
        assert row["source_skill"] == "old_skill"

    def test_message_only(self, tmp_path):
        db = _make_db(tmp_path)
        assert _run(db, "5", "--message", "new message") == 0
        row = _row(db, 5)
        assert row["message"] == "new message"
        assert row["grep_pattern"] == "old_pattern"
        assert row["file_glob"] == "old_glob"
        assert row["is_blocking"] == 0
        assert row["source_skill"] == "old_skill"

    def test_blocking_flag_sets_is_blocking_true(self, tmp_path):
        db = _make_db(tmp_path)
        assert _run(db, "5", "--blocking") == 0
        assert _row(db, 5)["is_blocking"] == 1

    def test_no_blocking_flag_sets_is_blocking_false(self, tmp_path):
        db = _make_db(tmp_path)
        # Seed as blocking first, then flip it off via --no-blocking.
        conn = sqlite3.connect(db)
        conn.execute("UPDATE lint_rules SET is_blocking = 1 WHERE id = 5")
        conn.commit()
        conn.close()
        assert _run(db, "5", "--no-blocking") == 0
        assert _row(db, 5)["is_blocking"] == 0

    def test_blocking_and_no_blocking_are_mutually_exclusive(self, tmp_path):
        db = _make_db(tmp_path)
        with pytest.raises(SystemExit):
            _run(db, "5", "--blocking", "--no-blocking")

    def test_skill_only(self, tmp_path):
        db = _make_db(tmp_path)
        assert _run(db, "5", "--skill", "new_skill") == 0
        row = _row(db, 5)
        assert row["source_skill"] == "new_skill"
        assert row["message"] == "old_message"

    def test_multiple_fields_in_one_call(self, tmp_path):
        db = _make_db(tmp_path)
        assert _run(
            db, "5",
            "--file-glob", "x/*.py",
            "--message", "msg",
            "--blocking",
        ) == 0
        row = _row(db, 5)
        assert row["file_glob"] == "x/*.py"
        assert row["message"] == "msg"
        assert row["is_blocking"] == 1
        # unspecified columns untouched
        assert row["grep_pattern"] == "old_pattern"
        assert row["source_skill"] == "old_skill"


class TestPreservation:
    def test_id_and_created_at_preserved(self, tmp_path):
        db = _make_db(tmp_path)
        original = _row(db, 5)
        assert _run(db, "5", "--message", "edited") == 0
        updated = _row(db, 5)
        assert updated["id"] == original["id"] == 5
        assert updated["created_at"] == original["created_at"]


class TestErrorPaths:
    def test_unknown_id_exits_2(self, tmp_path, capsys):
        db = _make_db(tmp_path)
        exit_code = _run(db, "99999", "--message", "x")
        assert exit_code == 2
        captured = capsys.readouterr()
        assert "99999" in captured.err
        assert "not found" in captured.err

    def test_no_flags_exits_2(self, tmp_path, capsys):
        db = _make_db(tmp_path)
        exit_code = _run(db, "5")
        assert exit_code == 2
        assert "no fields to update" in capsys.readouterr().err
        # row must be unchanged
        row = _row(db, 5)
        assert row["message"] == "old_message"
        assert row["file_glob"] == "old_glob"
