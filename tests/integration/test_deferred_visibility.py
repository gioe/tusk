"""Regression test for Issue #584: a deferred To Do task with no blockers
must surface in v_ready_tasks AND be selectable by `tusk task-select`.

Migration 59 added an ``is_deferred`` filter to v_ready_tasks and
v_chain_heads, which created a hidden third state where deferred tasks were
``status='To Do'`` yet invisible to ``/tusk``, ``/tusk blocked``, and
``/loop``. Migration 61 reverts that. This test pins the post-v61 behavior
end-to-end:

- The view itself surfaces the deferred row.
- The `tusk task-select` CLI (which queries v_ready_tasks via
  ``tusk-rank-lib.select_top_ready_task``) returns the deferred task when
  it is the only ready candidate — proving /tusk's no-arg picker, /loop,
  and tusk deps ready can all reach it.
"""

import json
import os
import sqlite3
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _close_done(conn, task_id):
    """Mark a task Done so it stops competing as a ready candidate.

    `db_path` ships fresh, but defensive helper for any test that needs to
    isolate a single deferred candidate.
    """
    conn.execute(
        "UPDATE tasks SET status = 'Done', closed_reason = 'completed' WHERE id = ?",
        (task_id,),
    )
    conn.commit()


def _insert_deferred_todo(conn, *, summary="[Deferred] regression candidate", priority_score=50):
    cur = conn.execute(
        """
        INSERT INTO tasks (summary, status, priority, complexity, task_type,
                           priority_score, is_deferred)
        VALUES (?, 'To Do', 'Medium', 'S', 'feature', ?, 1)
        """,
        (summary, priority_score),
    )
    conn.commit()
    return cur.lastrowid


def _insert_acceptance_criterion(conn, task_id):
    """Add a single criterion so `tusk task-start` doesn't refuse with
    'no acceptance criteria' (the picker filters tasks the same way)."""
    conn.execute(
        "INSERT INTO acceptance_criteria (task_id, criterion, source, is_completed)"
        " VALUES (?, 'placeholder', 'original', 0)",
        (task_id,),
    )
    conn.commit()


class TestDeferredVisibility:

    def test_deferred_task_appears_in_v_ready_tasks(self, db_path):
        """Direct view check: a deferred 'To Do' row with no blockers is in
        v_ready_tasks. This is the primary fix described in Issue #584."""
        db = str(db_path)
        conn = sqlite3.connect(db)
        try:
            deferred_id = _insert_deferred_todo(conn)
            ids = {r[0] for r in conn.execute("SELECT id FROM v_ready_tasks").fetchall()}
        finally:
            conn.close()

        assert deferred_id in ids, (
            "deferred To Do task with no blockers must appear in v_ready_tasks "
            "(reverses migration 59's hidden-third-state filter)"
        )

    def test_tusk_task_select_returns_deferred_when_only_candidate(self, db_path):
        """End-to-end: `tusk task-select` (the picker behind /tusk's no-arg
        path, /loop, and tusk deps ready) returns the deferred row when it
        is the only ready candidate. Proves the view fix propagates through
        tusk-rank-lib.select_top_ready_task."""
        db = str(db_path)
        conn = sqlite3.connect(db)
        try:
            deferred_id = _insert_deferred_todo(conn, priority_score=80)
            _insert_acceptance_criterion(conn, deferred_id)
        finally:
            conn.close()

        result = subprocess.run(
            [TUSK_BIN, "task-select"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "TUSK_DB": db},
        )

        assert result.returncode == 0, (
            "task-select should return the deferred candidate, not exit 1 "
            f"(stdout={result.stdout!r}, stderr={result.stderr!r})"
        )
        payload = json.loads(result.stdout)
        assert payload["id"] == deferred_id, (
            f"task-select picked id={payload.get('id')} but the only ready candidate "
            f"is the deferred row id={deferred_id}"
        )

    def test_non_deferred_outranks_deferred_when_both_ready(self, db_path):
        """Sanity check: removing the filter does NOT mean deferred wins —
        WSJF still applies a non_deferred_bonus, so a non-deferred task at
        the same base priority still ranks higher. This guards against an
        accidental over-correction where deferred tasks become preferred."""
        db = str(db_path)
        conn = sqlite3.connect(db)
        try:
            deferred_id = _insert_deferred_todo(conn, priority_score=50)
            _insert_acceptance_criterion(conn, deferred_id)

            # Non-deferred peer at the same base priority but with the
            # WSJF non_deferred_bonus baked into priority_score (deferred
            # gets +0; non-deferred gets +10 in the WSJF formula). We
            # express that delta directly via priority_score so the test
            # does not depend on a separate `tusk wsjf` recompute pass.
            cur = conn.execute(
                """
                INSERT INTO tasks (summary, status, priority, complexity, task_type,
                                   priority_score, is_deferred)
                VALUES ('non-deferred peer', 'To Do', 'Medium', 'S', 'feature', 60, 0)
                """,
            )
            non_deferred_id = cur.lastrowid
            _insert_acceptance_criterion(conn, non_deferred_id)
            conn.commit()
        finally:
            conn.close()

        result = subprocess.run(
            [TUSK_BIN, "task-select"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "TUSK_DB": db},
        )
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["id"] == non_deferred_id, (
            "non-deferred peer at higher priority_score must outrank the deferred "
            f"candidate; picker returned id={payload.get('id')}"
        )
