"""Unit tests for rule9_deferred_missing_expiry and rule10_criteria_type_mismatch.

Covers the migration from subprocess-based tusk CLI calls to direct SQLite
connections via tusk_loader + tusk-db-lib. Confirms the rules return the same
violation set for the same input and degrade gracefully when the DB is
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


def _make_tasks_db(tmp_dir, tasks):
    """tasks: list of (id, summary, status, is_deferred, expires_at)."""
    db_path = os.path.join(tmp_dir, "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tasks"
        " (id INTEGER PRIMARY KEY, summary TEXT, status TEXT,"
        "  is_deferred INTEGER, expires_at TEXT)"
    )
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)", tasks)
    conn.commit()
    conn.close()
    return db_path


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


class TestRule9DeferredMissingExpiry:
    def test_deferred_without_expiry_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_tasks_db(
                tmp,
                tasks=[(1, "Deferred item", "To Do", 1, None)],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                violations = lint.rule9_deferred_missing_expiry(tmp)
        assert len(violations) == 1
        assert "TASK-1" in violations[0]
        assert "Deferred item" in violations[0]

    def test_deferred_with_expiry_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_tasks_db(
                tmp,
                tasks=[(2, "Has expiry", "To Do", 1, "2026-12-31")],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule9_deferred_missing_expiry(tmp) == []

    def test_non_deferred_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_tasks_db(
                tmp,
                tasks=[(3, "Active work", "To Do", 0, None)],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule9_deferred_missing_expiry(tmp) == []

    def test_done_task_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_tasks_db(
                tmp,
                tasks=[(4, "Completed deferred", "Done", 1, None)],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule9_deferred_missing_expiry(tmp) == []

    def test_multiple_violations_ordered_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_tasks_db(
                tmp,
                tasks=[
                    (10, "Second", "To Do", 1, None),
                    (5, "First", "To Do", 1, None),
                ],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                violations = lint.rule9_deferred_missing_expiry(tmp)
        assert len(violations) == 2
        assert "TASK-5" in violations[0]
        assert "TASK-10" in violations[1]

    def test_db_unavailable_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(lint, "_db_path_from_root", return_value=None):
                assert lint.rule9_deferred_missing_expiry(tmp) == []

    def test_missing_table_returns_empty(self):
        """Pre-migration DB without a tasks table shouldn't crash the rule."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "tasks.db")
            conn = sqlite3.connect(db_path)
            conn.close()
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule9_deferred_missing_expiry(tmp) == []


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
