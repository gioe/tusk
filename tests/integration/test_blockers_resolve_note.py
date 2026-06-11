"""Integration tests for `tusk blockers resolve --note` (issue #1046).

Exercises the real CLI against the isolated DB from the shared db_path
fixture: --note stores the rationale on the blocker row, surfaces in the
list/all output, and resolve without --note keeps working unchanged.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(*argv):
    return subprocess.run([TUSK_BIN, *argv], capture_output=True, text=True)


def _insert_task(db_path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score)"
            " VALUES ('blocker host task', 'To Do', 'feature', 'Medium', 'S', 50)"
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _add_blocker(db_path, task_id) -> int:
    result = _run("blockers", "add", str(task_id), "repro blocker", "--type", "infra")
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT MAX(id) FROM external_blockers").fetchone()[0]
    finally:
        conn.close()


def _blocker_row(db_path, blocker_id):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT is_resolved, resolution_note FROM external_blockers WHERE id = ?",
            (blocker_id,),
        ).fetchone()
    finally:
        conn.close()


def test_resolve_with_note_stores_rationale(db_path):
    task_id = _insert_task(db_path)
    blocker_id = _add_blocker(db_path, task_id)

    result = _run("blockers", "resolve", str(blocker_id), "--note", "resolved by run XYZ")
    assert result.returncode == 0, result.stderr
    assert "resolved by run XYZ" in result.stdout

    row = _blocker_row(db_path, blocker_id)
    assert row["is_resolved"] == 1
    assert row["resolution_note"] == "resolved by run XYZ"


def test_resolve_without_note_unchanged(db_path):
    task_id = _insert_task(db_path)
    blocker_id = _add_blocker(db_path, task_id)

    result = _run("blockers", "resolve", str(blocker_id))
    assert result.returncode == 0, result.stderr

    row = _blocker_row(db_path, blocker_id)
    assert row["is_resolved"] == 1
    assert row["resolution_note"] is None


def test_note_surfaces_in_list_and_all(db_path):
    task_id = _insert_task(db_path)
    blocker_id = _add_blocker(db_path, task_id)
    _run("blockers", "resolve", str(blocker_id), "--note", "GHA run 27085866689 proved token fixed")

    list_out = _run("blockers", "list", str(task_id))
    assert list_out.returncode == 0, list_out.stderr
    assert "GHA run 27085866689 proved token fixed" in list_out.stdout

    all_out = _run("blockers", "all")
    assert all_out.returncode == 0, all_out.stderr
    assert "GHA run 27085866689 proved token fixed" in all_out.stdout


def test_note_on_already_resolved_blocker_is_ignored(db_path):
    task_id = _insert_task(db_path)
    blocker_id = _add_blocker(db_path, task_id)
    _run("blockers", "resolve", str(blocker_id), "--note", "first rationale")

    result = _run("blockers", "resolve", str(blocker_id), "--note", "second rationale")
    assert result.returncode == 0, result.stderr
    assert "already resolved" in result.stdout
    assert "--note ignored" in result.stdout

    row = _blocker_row(db_path, blocker_id)
    assert row["resolution_note"] == "first rationale"
