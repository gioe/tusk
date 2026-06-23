"""Unit tests for tusk-chain.py multi-head behavior with non-converging strands.

Objectives routinely decompose into independent dependency strands (A->B, C->D,
E standalone). /objective Step 4a hands the union of those ready heads to /chain,
which calls `tusk chain scope|frontier|frontier-check|validate-scope|status`.
Before issue #1133 those calls were rejected by a `validate_multi_head` guard that
required the heads to converge on a shared downstream task. This module verifies:

- the convergence guard is gone (no `validate_multi_head` symbol survives)
- every command function computes the union of per-head sub-DAGs for disjoint heads
- converging multi-head sets still resolve to the same union (unchanged behavior)
- frontier-check still reports continue/stuck/complete correctly across strands
"""

import importlib.util
import json
import os
import sqlite3
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_chain",
    os.path.join(BIN, "tusk-chain.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ── helpers ───────────────────────────────────────────────────────────


def _make_conn(tasks, deps=None, external_blockers=None):
    """Build an in-memory DB with the columns tusk-chain.py reads.

    tasks: list of (id, status) or (id, status, summary); other columns defaulted.
    deps: list of (task_id, depends_on_id) — relationship_type defaults to 'blocks'.
    external_blockers: list of (task_id, is_resolved).
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            summary TEXT,
            status TEXT,
            priority TEXT,
            complexity TEXT,
            assignee TEXT,
            description TEXT
        );
        CREATE TABLE task_dependencies (
            task_id INTEGER,
            depends_on_id INTEGER,
            relationship_type TEXT DEFAULT 'blocks'
        );
        CREATE TABLE external_blockers (
            task_id INTEGER,
            is_resolved INTEGER DEFAULT 0
        );
        """
    )
    for row in tasks:
        tid, status = row[0], row[1]
        summary = row[2] if len(row) > 2 else f"task {tid}"
        conn.execute(
            "INSERT INTO tasks (id, summary, status, priority, complexity, assignee, description) "
            "VALUES (?, ?, ?, 'Medium', 'S', NULL, '')",
            (tid, summary, status),
        )
    for task_id, depends_on_id in deps or []:
        conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_id, relationship_type) VALUES (?, ?, 'blocks')",
            (task_id, depends_on_id),
        )
    for task_id, is_resolved in external_blockers or []:
        conn.execute(
            "INSERT INTO external_blockers (task_id, is_resolved) VALUES (?, ?)",
            (task_id, is_resolved),
        )
    conn.commit()
    return conn


def _capture_json(capsys, fn, *args):
    fn(*args)
    out = capsys.readouterr().out.strip()
    return json.loads(out)


# Three disjoint strands: 1->2, 3->4, 5 standalone. Heads = [1, 3, 5].
DISJOINT_TASKS_TODO = [(1, "To Do"), (2, "To Do"), (3, "To Do"), (4, "To Do"), (5, "To Do")]
DISJOINT_DEPS = [(2, 1), (4, 3)]
DISJOINT_HEADS = [1, 3, 5]


# ── the rejection guard is gone ───────────────────────────────────────


def test_validate_multi_head_symbol_removed():
    """The convergence-requiring guard must no longer exist (issue #1133)."""
    assert not hasattr(mod, "validate_multi_head")


# ── disjoint-strand scope/frontier ────────────────────────────────────


def test_scope_unions_disjoint_strands(capsys):
    conn = _make_conn(DISJOINT_TASKS_TODO, DISJOINT_DEPS)
    result = _capture_json(capsys, mod.cmd_scope, conn, DISJOINT_HEADS)
    assert result["head_task_ids"] == DISJOINT_HEADS
    assert result["total_tasks"] == 5
    assert {t["id"] for t in result["tasks"]} == {1, 2, 3, 4, 5}
    # heads at depth 0, their dependents at depth 1
    depth = {t["id"]: t["depth"] for t in result["tasks"]}
    assert depth == {1: 0, 3: 0, 5: 0, 2: 1, 4: 1}


def test_bfs_downstream_union_disjoint():
    conn = _make_conn(DISJOINT_TASKS_TODO, DISJOINT_DEPS)
    union = dict(mod.bfs_downstream_union(conn, DISJOINT_HEADS))
    assert union == {1: 0, 2: 1, 3: 0, 4: 1, 5: 0}


def test_frontier_disjoint_strands_returns_all_ready_heads(capsys):
    conn = _make_conn(DISJOINT_TASKS_TODO, DISJOINT_DEPS)
    result = _capture_json(capsys, mod.cmd_frontier, conn, DISJOINT_HEADS)
    # only the unblocked roots of each strand are ready; dependents stay blocked
    assert {t["id"] for t in result["frontier"]} == {1, 3, 5}


def test_validate_scope_disjoint_strands_active_chain(capsys):
    conn = _make_conn(DISJOINT_TASKS_TODO, DISJOINT_DEPS)
    result = _capture_json(capsys, mod.cmd_validate_scope, conn, DISJOINT_HEADS)
    assert result == {"scope_type": "active-chain", "skip_head_execution": False}


def test_status_disjoint_strands_counts_union(capsys):
    conn = _make_conn(DISJOINT_TASKS_TODO, DISJOINT_DEPS)
    result = _capture_json(capsys, mod.cmd_status, conn, DISJOINT_HEADS)
    assert result["totals"]["total"] == 5
    assert [h["id"] for h in result["heads"]] == DISJOINT_HEADS


# ── frontier-check continue/stuck/complete across strands ─────────────


def test_frontier_check_continue_disjoint(capsys):
    conn = _make_conn(DISJOINT_TASKS_TODO, DISJOINT_DEPS)
    result = _capture_json(capsys, mod.cmd_frontier_check, conn, DISJOINT_HEADS)
    assert result["status"] == "continue"
    assert {t["id"] for t in result["frontier"]} == {1, 3, 5}


def test_frontier_check_complete_disjoint(capsys):
    all_done = [(tid, "Done") for tid in (1, 2, 3, 4, 5)]
    conn = _make_conn(all_done, DISJOINT_DEPS)
    result = _capture_json(capsys, mod.cmd_frontier_check, conn, DISJOINT_HEADS)
    assert result["status"] == "complete"
    assert result["frontier"] == []


def test_frontier_check_stuck_disjoint(capsys):
    # strand 1->2 with 1 Done, 2 To Do but held by an unresolved external blocker;
    # standalone 5 Done. Heads [1, 5]: tasks remain (2) but the frontier is empty.
    tasks = [(1, "Done"), (2, "To Do"), (5, "Done")]
    conn = _make_conn(tasks, deps=[(2, 1)], external_blockers=[(2, 0)])
    result = _capture_json(capsys, mod.cmd_frontier_check, conn, [1, 5])
    assert result["status"] == "stuck"
    assert result["frontier"] == []


# ── converging multi-head sets unchanged ──────────────────────────────


def test_scope_converging_heads_unchanged(capsys):
    # 1->3, 2->3 : heads 1 and 2 converge on shared dependent 3.
    tasks = [(1, "To Do"), (2, "To Do"), (3, "To Do")]
    conn = _make_conn(tasks, deps=[(3, 1), (3, 2)])
    result = _capture_json(capsys, mod.cmd_scope, conn, [1, 2])
    assert {t["id"] for t in result["tasks"]} == {1, 2, 3}
    depth = {t["id"]: t["depth"] for t in result["tasks"]}
    assert depth == {1: 0, 2: 0, 3: 1}
