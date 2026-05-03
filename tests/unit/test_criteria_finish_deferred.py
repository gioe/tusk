"""Regression test for `tusk criteria finish-deferred` reason scoping.

The `--reason` argument must be a strict equality filter against
`acceptance_criteria.deferred_reason`. Any deferred criterion whose reason
does NOT match must be left untouched. This guards the chain-orchestrator
workflow (which relies on `--reason chain` to bulk-finalize only its own
deferrals) from being broken by the more-general use of `tusk criteria skip`
for not-applicable criteria with arbitrary free-text reasons (TASK-281,
issue #618).
"""

import argparse
import importlib.util
import io
import os
import sqlite3
from contextlib import redirect_stdout
from unittest.mock import patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_criteria",
    os.path.join(REPO_ROOT, "bin", "tusk-criteria.py"),
)
criteria_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(criteria_mod)


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, summary TEXT, "
        "status TEXT DEFAULT 'In Progress')"
    )
    conn.execute(
        "CREATE TABLE acceptance_criteria ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  task_id INTEGER, criterion TEXT,"
        "  is_completed INTEGER DEFAULT 0, is_deferred INTEGER DEFAULT 0,"
        "  deferred_reason TEXT,"
        "  completed_at TEXT, updated_at TEXT, created_at TEXT"
        ")"
    )
    conn.execute("INSERT INTO tasks (id, summary) VALUES (1, 'Test')")
    return conn


def _seed(conn, rows):
    """rows: list of (criterion, is_deferred, deferred_reason, is_completed)."""
    for criterion, deferred, reason, completed in rows:
        conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, is_deferred, deferred_reason, is_completed) "
            "VALUES (1, ?, ?, ?, ?)",
            (criterion, deferred, reason, completed),
        )
    conn.commit()


class _NoCloseConn:
    """Proxy that delegates everything to the wrapped sqlite3.Connection but
    swallows .close() so the test can keep querying after cmd_finish_deferred
    exits its `try/finally: conn.close()` block."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        return None

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _run_finish_deferred(conn, reason, task_ids):
    """Invoke cmd_finish_deferred against the in-memory conn.

    cmd_finish_deferred opens its own connection via get_connection() and
    closes it on exit; patch get_connection to return a proxy that swallows
    .close() so the test can read mutations afterward.
    """
    args = argparse.Namespace(reason=reason, task_ids=task_ids)
    out = io.StringIO()
    with patch.object(criteria_mod, "get_connection", return_value=_NoCloseConn(conn)), \
         redirect_stdout(out):
        rc = criteria_mod.cmd_finish_deferred(args, ":memory:", {})
    return rc, out.getvalue()


def test_finish_deferred_only_matches_exact_reason():
    """The chain-deferred criterion is converted; the not-applicable and other-reason
    criteria are left as is_deferred=1 / is_completed=0."""
    conn = _make_db()
    _seed(conn, [
        ("Wired into chain consolidator commit", 1, "chain", 0),
        ("Document why exempt", 1, "not applicable: chose rate-limiting", 0),
        ("Other deferred path", 1, "out of scope — see TASK-999", 0),
        ("Already completed normally", 0, None, 1),
    ])

    rc, _ = _run_finish_deferred(conn, "chain", [1])
    assert rc == 0

    rows = conn.execute(
        "SELECT criterion, is_completed, is_deferred, deferred_reason "
        "FROM acceptance_criteria WHERE task_id = 1 ORDER BY id"
    ).fetchall()

    # 1. chain-deferred → converted to completed (is_deferred unchanged in current
    #    implementation, but the gate already excludes is_deferred so the column's
    #    state is irrelevant; what matters is is_completed flipped to 1).
    assert rows[0]["is_completed"] == 1
    # 2/3. non-matching reasons left untouched
    assert rows[1]["is_completed"] == 0
    assert rows[1]["is_deferred"] == 1
    assert rows[1]["deferred_reason"] == "not applicable: chose rate-limiting"
    assert rows[2]["is_completed"] == 0
    assert rows[2]["is_deferred"] == 1
    # 4. already-completed-normally row untouched
    assert rows[3]["is_completed"] == 1


def test_finish_deferred_no_match_returns_zero_marked():
    """When no criterion matches the reason, command exits 0 and JSON reports marked=0."""
    conn = _make_db()
    _seed(conn, [
        ("Not applicable", 1, "not applicable: chose rate-limiting", 0),
    ])

    rc, output = _run_finish_deferred(conn, "chain", [1])
    assert rc == 0
    assert '"marked": 0' in output

    row = conn.execute(
        "SELECT is_completed, is_deferred FROM acceptance_criteria WHERE id = 1"
    ).fetchone()
    assert row["is_completed"] == 0
    assert row["is_deferred"] == 1


def test_finish_deferred_scoped_to_task_ids():
    """A matching criterion on a different task is not affected when its task_id
    is not passed."""
    conn = _make_db()
    conn.execute("INSERT INTO tasks (id, summary) VALUES (2, 'Other')")
    _seed(conn, [
        ("Chain on task 1", 1, "chain", 0),
    ])
    conn.execute(
        "INSERT INTO acceptance_criteria "
        "(task_id, criterion, is_deferred, deferred_reason, is_completed) "
        "VALUES (2, 'Chain on task 2', 1, 'chain', 0)"
    )
    conn.commit()

    rc, _ = _run_finish_deferred(conn, "chain", [1])  # only task 1
    assert rc == 0

    rows = conn.execute(
        "SELECT task_id, is_completed FROM acceptance_criteria ORDER BY id"
    ).fetchall()
    assert (rows[0]["task_id"], rows[0]["is_completed"]) == (1, 1)
    assert (rows[1]["task_id"], rows[1]["is_completed"]) == (2, 0)
