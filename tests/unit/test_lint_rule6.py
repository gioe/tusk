"""Unit tests for rule6_done_incomplete_criteria in tusk-lint.py.

Covers the 30-day scoping filter: historical Done tasks with retroactively-added
incomplete criteria must not be flagged, but recently-closed Done tasks with
incomplete criteria still must be.
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


def _make_db(tmp_dir, tasks, criteria):
    """Create a minimal SQLite DB with tasks and acceptance_criteria tables.

    tasks: list of (id, summary, status, closed_at, updated_at)
    criteria: list of (id, task_id, is_completed, is_deferred)
    """
    db_path = os.path.join(tmp_dir, "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tasks"
        " (id INTEGER PRIMARY KEY, summary TEXT, status TEXT,"
        "  closed_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE acceptance_criteria"
        " (id INTEGER PRIMARY KEY, task_id INTEGER,"
        "  is_completed INTEGER, is_deferred INTEGER)"
    )
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)", tasks)
    conn.executemany("INSERT INTO acceptance_criteria VALUES (?, ?, ?, ?)", criteria)
    conn.commit()
    conn.close()
    return db_path


class TestRule6Scoping:
    def test_historical_done_task_not_flagged(self):
        """Done task closed 100 days ago with retroactive incomplete criteria is skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(
                tmp,
                tasks=[(1, "Ancient task", "Done",
                        "2025-01-01 00:00:00", "2026-04-01 00:00:00")],
                criteria=[(1, 1, 0, 0)],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule6_done_incomplete_criteria(tmp) == []

    def test_recent_done_task_with_incomplete_criterion_flagged(self):
        """Done task closed within the 30-day window is still flagged."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(
                tmp,
                tasks=[(42, "Recent regression", "Done",
                        "2026-04-10 00:00:00", "2026-04-10 00:00:00")],
                criteria=[(1, 42, 0, 0)],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                violations = lint.rule6_done_incomplete_criteria(tmp)
        assert len(violations) == 1
        assert "TASK-42" in violations[0]
        assert "Recent regression" in violations[0]

    def test_recent_done_task_all_criteria_complete_not_flagged(self):
        """Done task with all criteria complete is not flagged regardless of date."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(
                tmp,
                tasks=[(5, "Finished task", "Done",
                        "2026-04-15 00:00:00", "2026-04-15 00:00:00")],
                criteria=[(1, 5, 1, 0)],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule6_done_incomplete_criteria(tmp) == []

    def test_deferred_criterion_not_counted(self):
        """Deferred criteria don't count as incomplete violations."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(
                tmp,
                tasks=[(7, "Deferred work", "Done",
                        "2026-04-10 00:00:00", "2026-04-10 00:00:00")],
                criteria=[(1, 7, 0, 1)],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule6_done_incomplete_criteria(tmp) == []

    def test_non_done_task_not_flagged(self):
        """In-progress tasks with incomplete criteria are ignored by rule 6."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(
                tmp,
                tasks=[(9, "Active work", "In Progress",
                        None, "2026-04-10 00:00:00")],
                criteria=[(1, 9, 0, 0)],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule6_done_incomplete_criteria(tmp) == []

    def test_null_closed_at_falls_back_to_updated_at(self):
        """When closed_at is NULL, updated_at is used for the recency check."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_db(
                tmp,
                tasks=[
                    (10, "Recent no-closed_at", "Done", None, "2026-04-15 00:00:00"),
                    (11, "Old no-closed_at", "Done", None, "2025-01-01 00:00:00"),
                ],
                criteria=[(1, 10, 0, 0), (2, 11, 0, 0)],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                violations = lint.rule6_done_incomplete_criteria(tmp)
        assert len(violations) == 1
        assert "TASK-10" in violations[0]
        assert "TASK-11" not in violations[0]

    def test_many_historical_violations_produce_no_output(self):
        """A large backlog of retroactive historical violations returns [] —
        this is the scenario that previously hung `tusk commit` for minutes."""
        with tempfile.TemporaryDirectory() as tmp:
            tasks = [
                (i, f"Historical task {i}", "Done",
                 "2024-06-01 00:00:00", "2024-06-01 00:00:00")
                for i in range(1, 151)
            ]
            criteria = [(i, i, 0, 0) for i in range(1, 151)]
            db_path = _make_db(tmp, tasks=tasks, criteria=criteria)
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule6_done_incomplete_criteria(tmp) == []

    def test_db_unavailable_returns_empty(self):
        """Returns [] gracefully when the DB cannot be resolved."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(lint, "_db_path_from_root", return_value=None):
                assert lint.rule6_done_incomplete_criteria(tmp) == []
