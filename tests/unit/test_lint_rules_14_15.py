"""Unit tests for rule14_deferred_prefix_mismatch and rule15_big_bang_commits.

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
    """tasks: list of (id, summary, status, is_deferred)."""
    db_path = os.path.join(tmp_dir, "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tasks"
        " (id INTEGER PRIMARY KEY, summary TEXT, status TEXT, is_deferred INTEGER)"
    )
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?)", tasks)
    conn.commit()
    conn.close()
    return db_path


def _make_big_bang_db(tmp_dir, tasks, criteria):
    """
    tasks:    list of (id, summary, status)
    criteria: list of (id, task_id, is_completed, is_deferred, commit_hash)
    """
    db_path = os.path.join(tmp_dir, "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tasks"
        " (id INTEGER PRIMARY KEY, summary TEXT, status TEXT)"
    )
    conn.execute(
        "CREATE TABLE acceptance_criteria"
        " (id INTEGER PRIMARY KEY, task_id INTEGER,"
        "  is_completed INTEGER, is_deferred INTEGER, commit_hash TEXT)"
    )
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?)", tasks)
    conn.executemany(
        "INSERT INTO acceptance_criteria VALUES (?, ?, ?, ?, ?)", criteria
    )
    conn.commit()
    conn.close()
    return db_path


class TestRule14DeferredPrefixMismatch:
    def test_prefix_without_flag_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_tasks_db(
                tmp,
                tasks=[(1, "[Deferred] Forgot flag", "To Do", 0)],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                violations = lint.rule14_deferred_prefix_mismatch(tmp)
        assert len(violations) == 1
        assert "TASK-1" in violations[0]
        assert "[Deferred] Forgot flag" in violations[0]

    def test_flag_without_prefix_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_tasks_db(
                tmp,
                tasks=[(2, "Forgot prefix", "To Do", 1)],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                violations = lint.rule14_deferred_prefix_mismatch(tmp)
        assert len(violations) == 1
        assert "TASK-2" in violations[0]
        assert "Forgot prefix" in violations[0]

    def test_consistent_prefix_and_flag_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_tasks_db(
                tmp,
                tasks=[(3, "[Deferred] Consistent", "To Do", 1)],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule14_deferred_prefix_mismatch(tmp) == []

    def test_neither_prefix_nor_flag_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_tasks_db(
                tmp,
                tasks=[(4, "Normal task", "To Do", 0)],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule14_deferred_prefix_mismatch(tmp) == []

    def test_done_task_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_tasks_db(
                tmp,
                tasks=[(5, "Closed mismatch", "Done", 1)],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule14_deferred_prefix_mismatch(tmp) == []

    def test_multiple_violations_ordered_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_tasks_db(
                tmp,
                tasks=[
                    (10, "[Deferred] Second", "To Do", 0),
                    (5, "Flag only first", "To Do", 1),
                ],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                violations = lint.rule14_deferred_prefix_mismatch(tmp)
        assert len(violations) == 2
        assert "TASK-5" in violations[0]
        assert "TASK-10" in violations[1]

    def test_db_unavailable_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(lint, "_db_path_from_root", return_value=None):
                assert lint.rule14_deferred_prefix_mismatch(tmp) == []

    def test_missing_table_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "tasks.db")
            conn = sqlite3.connect(db_path)
            conn.close()
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule14_deferred_prefix_mismatch(tmp) == []


class TestRule15BigBangCommits:
    def test_all_criteria_on_one_commit_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_big_bang_db(
                tmp,
                tasks=[(1, "Big bang", "In Progress")],
                criteria=[
                    (101, 1, 1, 0, "abc123"),
                    (102, 1, 1, 0, "abc123"),
                    (103, 1, 1, 0, "abc123"),
                ],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                violations = lint.rule15_big_bang_commits(tmp)
        assert len(violations) == 1
        assert "TASK-1" in violations[0]
        assert "Big bang" in violations[0]
        assert "3" in violations[0]

    def test_criteria_on_distinct_commits_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_big_bang_db(
                tmp,
                tasks=[(2, "Staged work", "In Progress")],
                criteria=[
                    (201, 2, 1, 0, "hashA"),
                    (202, 2, 1, 0, "hashB"),
                ],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule15_big_bang_commits(tmp) == []

    def test_single_criterion_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_big_bang_db(
                tmp,
                tasks=[(3, "Solo", "In Progress")],
                criteria=[(301, 3, 1, 0, "solo")],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule15_big_bang_commits(tmp) == []

    def test_done_task_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_big_bang_db(
                tmp,
                tasks=[(4, "Already done", "Done")],
                criteria=[
                    (401, 4, 1, 0, "x"),
                    (402, 4, 1, 0, "x"),
                ],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule15_big_bang_commits(tmp) == []

    def test_deferred_criteria_ignored(self):
        """Deferred criteria are excluded from the grouping check."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_big_bang_db(
                tmp,
                tasks=[(5, "Mixed", "In Progress")],
                criteria=[
                    (501, 5, 1, 1, "same"),
                    (502, 5, 1, 0, "same"),
                ],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                # Only one non-deferred criterion remains — HAVING count > 1 fails.
                assert lint.rule15_big_bang_commits(tmp) == []

    def test_incomplete_criteria_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_big_bang_db(
                tmp,
                tasks=[(6, "Partly done", "In Progress")],
                criteria=[
                    (601, 6, 0, 0, "same"),
                    (602, 6, 1, 0, "same"),
                ],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule15_big_bang_commits(tmp) == []

    def test_null_commit_hash_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _make_big_bang_db(
                tmp,
                tasks=[(7, "No hashes", "In Progress")],
                criteria=[
                    (701, 7, 1, 0, None),
                    (702, 7, 1, 0, None),
                ],
            )
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule15_big_bang_commits(tmp) == []

    def test_db_unavailable_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(lint, "_db_path_from_root", return_value=None):
                assert lint.rule15_big_bang_commits(tmp) == []

    def test_missing_table_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "tasks.db")
            conn = sqlite3.connect(db_path)
            conn.close()
            with patch.object(lint, "_db_path_from_root", return_value=db_path):
                assert lint.rule15_big_bang_commits(tmp) == []
