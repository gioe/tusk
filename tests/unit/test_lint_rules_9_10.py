"""Unit tests for rule10_criteria_type_mismatch.

Covers the migration from subprocess-based tusk CLI calls to direct SQLite
connections via tusk_loader + tusk-db-lib. Confirms the rule returns the same
violation set for the same input and degrades gracefully when the DB is
unavailable or the expected tables don't exist.
"""

import importlib.util
import os
import sqlite3
import tempfile
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_lint",
    os.path.join(REPO_ROOT, "bin", "tusk-lint.py"),
)
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


def _make_criteria_db(tmp_dir, criteria):
    """criteria: list of (id, task_id, criterion, criterion_type, verification_spec)."""
    db_path = os.path.join(tmp_dir, "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE acceptance_criteria"
        " (id INTEGER PRIMARY KEY, task_id INTEGER, criterion TEXT,"
        "  criterion_type TEXT, verification_spec TEXT)"
    )
    conn.executemany(
        "INSERT INTO acceptance_criteria VALUES (?, ?, ?, ?, ?)", criteria
    )
    conn.commit()
    conn.close()
    return db_path


class TestRule10CriteriaTypeMismatch:
    def test_manual_with_verification_spec_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_criteria_db(
                tmp,
                criteria=[(1, 42, "Check X", "manual", "pytest -q")],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                violations = lint.rule10_criteria_type_mismatch(tmp)
        assert len(violations) == 1
        assert "criterion 1" in violations[0]
        assert "task 42" in violations[0]
        assert "Check X" in violations[0]

    def test_manual_without_spec_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_criteria_db(
                tmp,
                criteria=[(2, 43, "Pure manual", "manual", None)],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule10_criteria_type_mismatch(tmp) == []

    def test_non_manual_with_spec_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_criteria_db(
                tmp,
                criteria=[(3, 44, "Test criterion", "test", "pytest -q")],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule10_criteria_type_mismatch(tmp) == []

    def test_db_unavailable_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(lint, "_db_path_from_root", return_value=None):
                assert lint.rule10_criteria_type_mismatch(tmp) == []

    def test_missing_table_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "tasks.db")
            conn = sqlite3.connect(db_path)
            conn.close()
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule10_criteria_type_mismatch(tmp) == []
