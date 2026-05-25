"""Integration tests for `bin/tusk-insights.py` — worktree-pool health.

The script reports three counts derived from the task_workspaces registry
plus the live filesystem:

- ``reconcile_eligible``: task is Done AND workspace_path exists on disk.
- ``prune_eligible``: workspace_path is gone AND branch not in ``git
  worktree list`` (same predicate as ``_is_stale_workspace`` in
  ``tusk-task-worktree.py``).
- ``disk_usage_bytes``: sum of file sizes under every existing
  workspace_path in the registry.

Tests insert task_workspaces rows directly via sqlite3 — the schema is a
fixed contract and going through ``tusk task-worktree create`` would
require a real git repo per row, which is unnecessary for unit-level
health-count coverage.
"""

import json
import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
INSIGHTS = os.path.join(REPO_ROOT, "bin", "tusk-insights.py")
CONFIG = os.path.join(REPO_ROOT, "config.default.json")


def _insert_task(db_path, *, status="In Progress"):
    closed_reason = "completed" if status == "Done" else None
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, "
            "priority, complexity, priority_score, closed_reason) VALUES "
            "(?, 'desc', ?, 'feature', 'High', 'M', 30, ?)",
            ("insights health task", status, closed_reason),
        )
        conn.commit()
        return cur.lastrowid


def _insert_workspace(db_path, task_id, branch, workspace_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO task_workspaces (task_id, branch, workspace_path) "
            "VALUES (?, ?, ?)",
            (task_id, branch, str(workspace_path)),
        )
        conn.commit()


def _run(db_path, repo_root):
    result = subprocess.run(
        ["python3", INSIGHTS, str(db_path), CONFIG, str(repo_root),
         "--format", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"insights failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return json.loads(result.stdout)


def test_reconcile_count(db_path, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    # Done + on-disk → reconcile-eligible.
    done_task = _insert_task(db_path, status="Done")
    done_ws = tmp_path / "ws-done"
    done_ws.mkdir()
    _insert_workspace(db_path, done_task,
                      "feature/TASK-x-done", done_ws)

    # In Progress + on-disk → not reconcile-eligible (task still open).
    open_task = _insert_task(db_path, status="In Progress")
    open_ws = tmp_path / "ws-open"
    open_ws.mkdir()
    _insert_workspace(db_path, open_task,
                      "feature/TASK-x-open", open_ws)

    # Done + path missing → not reconcile-eligible (would be prune-eligible).
    gone_task = _insert_task(db_path, status="Done")
    _insert_workspace(db_path, gone_task,
                      "feature/TASK-x-gone", tmp_path / "missing")

    report = _run(db_path, repo_root)
    assert report["reconcile_eligible"] == 1
    assert any("task-worktree reconcile" in s for s in report["suggestions"])


def test_prune_count(db_path, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    # workspace_path absent AND not in `git worktree list` → prune-eligible.
    t1 = _insert_task(db_path, status="Done")
    _insert_workspace(db_path, t1,
                      "feature/TASK-x-stale1", tmp_path / "absent1")
    t2 = _insert_task(db_path, status="In Progress")
    _insert_workspace(db_path, t2,
                      "feature/TASK-x-stale2", tmp_path / "absent2")

    # workspace_path present → not prune-eligible (still real on disk).
    t3 = _insert_task(db_path, status="Done")
    alive = tmp_path / "alive"
    alive.mkdir()
    _insert_workspace(db_path, t3, "feature/TASK-x-alive", alive)

    report = _run(db_path, repo_root)
    assert report["prune_eligible"] == 2
    assert any("task-worktree prune" in s for s in report["suggestions"])


def test_disk_usage(db_path, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    t1 = _insert_task(db_path, status="Done")
    ws = tmp_path / "ws-sized"
    ws.mkdir()
    (ws / "a.txt").write_bytes(b"abcdef")          # 6 bytes
    (ws / "b.txt").write_bytes(b"hello world\n")   # 12 bytes
    sub = ws / "sub"
    sub.mkdir()
    (sub / "c.txt").write_bytes(b"x" * 1000)       # 1000 bytes
    _insert_workspace(db_path, t1, "feature/TASK-x-sized", ws)

    # Stale workspace must NOT inflate disk usage.
    t2 = _insert_task(db_path, status="Done")
    _insert_workspace(db_path, t2,
                      "feature/TASK-x-stale", tmp_path / "gone")

    report = _run(db_path, repo_root)
    assert report["disk_usage_bytes"] == 6 + 12 + 1000
