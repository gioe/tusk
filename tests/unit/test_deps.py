"""Unit tests for tusk-deps.py refactored functions.

Uses an in-memory SQLite DB — no filesystem or tmp_path required.
Covers: would_create_cycle, task_exists, get_task_summary,
        add_dependency, remove_dependency.
"""

import importlib.util
import io
import os
import sqlite3
from contextlib import redirect_stderr, redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load the module (hyphenated filename requires importlib)
_spec = importlib.util.spec_from_file_location(
    "tusk_deps",
    os.path.join(REPO_ROOT, "bin", "tusk-deps.py"),
)
deps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deps)


def make_db(*edges: tuple[int, int]) -> sqlite3.Connection:
    """Return an in-memory connection with only task_dependencies (for cycle tests)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE task_dependencies (task_id INTEGER, depends_on_id INTEGER)"
    )
    for task_id, depends_on_id in edges:
        conn.execute(
            "INSERT INTO task_dependencies VALUES (?, ?)", (task_id, depends_on_id)
        )
    conn.commit()
    return conn


def make_full_db(*task_rows: tuple[int, str]) -> sqlite3.Connection:
    """Return an in-memory connection with tasks + task_dependencies tables.

    task_rows: sequence of (id, summary) tuples to pre-populate.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, summary TEXT, status TEXT DEFAULT 'To Do')"
    )
    conn.execute(
        """CREATE TABLE task_dependencies (
            task_id INTEGER,
            depends_on_id INTEGER,
            relationship_type TEXT DEFAULT 'blocks',
            UNIQUE(task_id, depends_on_id)
        )"""
    )
    for task_id, summary in task_rows:
        conn.execute("INSERT INTO tasks VALUES (?, ?, 'To Do')", (task_id, summary))
    conn.commit()
    return conn


def capture(fn, *args, **kwargs):
    """Call fn(*args, **kwargs), capturing stdout/stderr. Returns (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(*args, **kwargs)
    return rc, out.getvalue(), err.getvalue()


class TestWouldCreateCycle:
    def test_simple_chain_detects_cycle(self):
        # A->B->C; adding B->A would create a cycle
        conn = make_db((1, 2), (2, 3))  # A=1, B=2, C=3
        assert deps.would_create_cycle(conn, 2, 1) is True

    def test_transitive_cycle_detected(self):
        # A->B->C; adding C->A would create a cycle
        conn = make_db((1, 2), (2, 3))
        assert deps.would_create_cycle(conn, 3, 1) is True

    def test_diamond_graph_adding_cycle_detected(self):
        # A->B, A->C, B->D, C->D; adding D->A would create a cycle
        conn = make_db((1, 2), (1, 3), (2, 4), (3, 4))
        assert deps.would_create_cycle(conn, 4, 1) is True

    def test_self_loop_detected(self):
        # A->A is a cycle
        conn = make_db()
        assert deps.would_create_cycle(conn, 1, 1) is True

    def test_simple_non_cycle_chain_returns_false(self):
        # A->B->C; adding A->C is fine (no cycle)
        conn = make_db((1, 2), (2, 3))
        assert deps.would_create_cycle(conn, 1, 3) is False

    def test_diamond_graph_no_cycle_returns_false(self):
        # A->B, A->C, B->D, C->D; D does not reach A, so A->D is fine
        conn = make_db((1, 2), (1, 3), (2, 4), (3, 4))
        assert deps.would_create_cycle(conn, 1, 4) is False

    def test_empty_graph_returns_false(self):
        conn = make_db()
        assert deps.would_create_cycle(conn, 1, 2) is False


# ── task_exists ───────────────────────────────────────────────────────


class TestTaskExists:
    def test_returns_true_for_existing_task(self):
        conn = make_full_db((1, "Task one"))
        assert deps.task_exists(conn, 1) is True

    def test_returns_false_for_missing_task(self):
        conn = make_full_db((1, "Task one"))
        assert deps.task_exists(conn, 99) is False

    def test_returns_false_on_empty_table(self):
        conn = make_full_db()
        assert deps.task_exists(conn, 1) is False


# ── get_task_summary ──────────────────────────────────────────────────


class TestGetTaskSummary:
    def test_returns_summary_for_existing_task(self):
        conn = make_full_db((1, "Fix the thing"))
        assert deps.get_task_summary(conn, 1) == "Fix the thing"

    def test_returns_none_for_missing_task(self):
        conn = make_full_db()
        assert deps.get_task_summary(conn, 42) is None


# ── add_dependency ────────────────────────────────────────────────────


class TestAddDependency:
    def test_happy_path_inserts_row(self):
        conn = make_full_db((1, "A"), (2, "B"))
        rc, out, _ = capture(deps.add_dependency, conn, 1, 2)
        assert rc == 0
        row = conn.execute(
            "SELECT * FROM task_dependencies WHERE task_id=1 AND depends_on_id=2"
        ).fetchone()
        assert row is not None
        assert row["relationship_type"] == "blocks"

    def test_contingent_type_is_stored(self):
        conn = make_full_db((1, "A"), (2, "B"))
        rc, _, _ = capture(deps.add_dependency, conn, 1, 2, "contingent")
        assert rc == 0
        row = conn.execute(
            "SELECT relationship_type FROM task_dependencies WHERE task_id=1 AND depends_on_id=2"
        ).fetchone()
        assert row["relationship_type"] == "contingent"

    def test_self_dependency_returns_error(self):
        conn = make_full_db((1, "A"))
        rc, _, err = capture(deps.add_dependency, conn, 1, 1)
        assert rc == 1
        assert "cannot depend on itself" in err

    def test_missing_task_id_returns_error(self):
        conn = make_full_db((2, "B"))
        rc, _, err = capture(deps.add_dependency, conn, 99, 2)
        assert rc == 1
        assert "does not exist" in err

    def test_missing_depends_on_id_returns_error(self):
        conn = make_full_db((1, "A"))
        rc, _, err = capture(deps.add_dependency, conn, 1, 99)
        assert rc == 1
        assert "does not exist" in err

    def test_invalid_relationship_type_returns_error(self):
        conn = make_full_db((1, "A"), (2, "B"))
        rc, _, err = capture(deps.add_dependency, conn, 1, 2, "unknown")
        assert rc == 1
        assert "Invalid relationship type" in err

    def test_cycle_detection_returns_error(self):
        conn = make_full_db((1, "A"), (2, "B"))
        # 2 depends on 1; adding 1 depends on 2 would create a cycle
        conn.execute(
            "INSERT INTO task_dependencies VALUES (2, 1, 'blocks')"
        )
        conn.commit()
        rc, _, err = capture(deps.add_dependency, conn, 1, 2)
        assert rc == 1
        assert "circular" in err

    def test_duplicate_dependency_is_silently_ignored(self):
        conn = make_full_db((1, "A"), (2, "B"))
        capture(deps.add_dependency, conn, 1, 2)
        # Second insert should not raise
        rc, out, _ = capture(deps.add_dependency, conn, 1, 2)
        assert rc == 0
        assert "already exists" in out


# ── remove_dependency ─────────────────────────────────────────────────


class TestRemoveDependency:
    def test_removes_existing_dependency(self):
        conn = make_full_db((1, "A"), (2, "B"))
        conn.execute("INSERT INTO task_dependencies VALUES (1, 2, 'blocks')")
        conn.commit()
        rc, out, _ = capture(deps.remove_dependency, conn, 1, 2)
        assert rc == 0
        row = conn.execute(
            "SELECT * FROM task_dependencies WHERE task_id=1 AND depends_on_id=2"
        ).fetchone()
        assert row is None
        assert "Removed" in out

    def test_removing_nonexistent_dependency_still_returns_zero(self):
        conn = make_full_db((1, "A"), (2, "B"))
        rc, out, _ = capture(deps.remove_dependency, conn, 1, 2)
        assert rc == 0
        assert "No dependency found" in out
