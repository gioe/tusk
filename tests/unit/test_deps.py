"""Unit tests for would_create_cycle() in tusk-deps.py.

Uses an in-memory SQLite DB — no filesystem or tmp_path required.
"""

import importlib.util
import os
import sqlite3

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
    """Return an in-memory connection pre-populated with the given edges.

    Each edge (a, b) means task_id=a depends on depends_on_id=b.
    """
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
