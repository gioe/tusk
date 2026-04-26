"""Regression test for Issue #586: `tusk task-list --format json` must
project the ``is_deferred`` column on every row.

Migration 61 dropped the deferred filter from v_ready_tasks/v_chain_heads,
so deferred tasks now compete in /tusk, /chain, and /loop on WSJF score.
JSON consumers (dashboards, /loop diagnostics, ad-hoc scripts) need
``is_deferred`` exposed in the listing to filter or highlight deferred
rows without N round-trips through ``tusk task-get <id>``.

This test pins the JSON shape so a future SELECT change cannot silently
drop the column again.
"""

import json
import os
import sqlite3
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _insert_task(conn, *, summary, is_deferred, priority_score=50):
    cur = conn.execute(
        """
        INSERT INTO tasks (summary, status, priority, complexity, task_type,
                           priority_score, is_deferred)
        VALUES (?, 'To Do', 'Medium', 'S', 'feature', ?, ?)
        """,
        (summary, priority_score, is_deferred),
    )
    conn.commit()
    return cur.lastrowid


class TestTaskListJsonIsDeferred:

    def test_is_deferred_present_on_every_row(self, db_path):
        """Every row in the JSON listing must include the is_deferred key."""
        db = str(db_path)
        conn = sqlite3.connect(db)
        try:
            _insert_task(conn, summary="non-deferred row", is_deferred=0)
            _insert_task(conn, summary="[Deferred] deferred row", is_deferred=1)
        finally:
            conn.close()

        result = subprocess.run(
            [TUSK_BIN, "task-list", "--format", "json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "TUSK_DB": db},
        )
        assert result.returncode == 0, (
            f"task-list --format json failed: stdout={result.stdout!r}, stderr={result.stderr!r}"
        )
        rows = json.loads(result.stdout)
        assert rows, "expected at least the two seeded rows"
        for row in rows:
            assert "is_deferred" in row, (
                f"row {row.get('id')} missing is_deferred — Issue #586 regression: "
                f"keys present = {sorted(row.keys())}"
            )

    def test_is_deferred_boolean_matches_underlying_column(self, db_path):
        """The JSON column must reflect the actual is_deferred value (0 or 1)
        — not a constant. Insert one of each and assert the booleans line up."""
        db = str(db_path)
        conn = sqlite3.connect(db)
        try:
            non_deferred_id = _insert_task(conn, summary="non-deferred row", is_deferred=0)
            deferred_id = _insert_task(conn, summary="[Deferred] deferred row", is_deferred=1)
        finally:
            conn.close()

        result = subprocess.run(
            [TUSK_BIN, "task-list", "--format", "json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "TUSK_DB": db},
        )
        assert result.returncode == 0
        by_id = {row["id"]: row for row in json.loads(result.stdout)}

        assert by_id[non_deferred_id]["is_deferred"] == 0
        assert by_id[deferred_id]["is_deferred"] == 1
