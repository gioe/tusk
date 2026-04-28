#!/usr/bin/env python3
"""tusk task-unstart: Revert a cleanly-orphaned In Progress task back to To Do.

Use when `tusk task-start` has handed back a task that turns out to be
non-actionable (e.g., a chained dep wasn't recorded, so the task isn't actually
ready). The status-transition trigger normally blocks `In Progress -> To Do`
because Done is terminal — this command bypasses that trigger only when the
task is *cleanly orphaned*: no progress checkpoints, no commits referencing
``[TASK-<id>]``, and no open session. Partially-worked tasks stay forward-only
and must close via task-done / merge / abandon.

Exit codes:
  0  reverted; JSON printed on stdout
  1  --force missing (confirmation hint printed)
  2  task not found, wrong status, or a guard fired (task_progress rows,
     [TASK-<id>] commits, or an open session)
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_git_helpers = tusk_loader.load("tusk-git-helpers")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
find_task_commits = _git_helpers.find_task_commits


def main(argv: list[str]) -> int:
    db_path = argv[0]
    # argv[1] is config_path (unused but kept for dispatch consistency)
    parser = argparse.ArgumentParser(
        prog="tusk task-unstart",
        description="Revert a cleanly-orphaned In Progress task back to To Do",
    )
    parser.add_argument("task_id", type=int, help="Task ID")
    parser.add_argument("--force", action="store_true", help="Confirm the reversal")
    args = parser.parse_args(argv[2:])
    task_id = args.task_id
    force = args.force

    if not force:
        print(
            f"This will revert task {task_id} from 'In Progress' back to 'To Do', clearing started_at.\n"
            "Refuses if the task has any progress checkpoints, [TASK-<id>] commits, or an open session.\n"
            "Re-run with --force to confirm:\n"
            f"  tusk task-unstart {task_id} --force",
            file=sys.stderr,
        )
        return 1

    # repo_root is two levels up from the DB: tusk/tasks.db -> tusk/ -> repo_root
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))

    conn = get_connection(db_path)
    try:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            print(f"Error: Task {task_id} not found.", file=sys.stderr)
            return 2

        if task["status"] != "In Progress":
            print(
                f"Error: Task {task_id} is '{task['status']}'. "
                "task-unstart only reverses 'In Progress' -> 'To Do'. "
                "Use task-reopen to reset Done or already-To-Do tasks.",
                file=sys.stderr,
            )
            return 2

        progress_rows = conn.execute(
            "SELECT COUNT(*) FROM task_progress WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        if progress_rows > 0:
            print(
                f"Error: Task {task_id} has {progress_rows} progress checkpoint(s). "
                "Cannot un-start a task with recorded work — close it via task-done or merge.",
                file=sys.stderr,
            )
            return 2

        task_commits = find_task_commits(task_id, repo_root, ["--all"])
        if task_commits:
            sample = ", ".join(c[:7] for c in task_commits[:3])
            more = f" (+{len(task_commits) - 3} more)" if len(task_commits) > 3 else ""
            print(
                f"Error: Task {task_id} has {len(task_commits)} git commit(s) referencing "
                f"[TASK-{task_id}]: {sample}{more}. "
                "Cannot un-start a task with recorded commits — close it via task-done or merge.",
                file=sys.stderr,
            )
            return 2

        open_sessions = conn.execute(
            "SELECT COUNT(*) FROM task_sessions WHERE task_id = ? AND ended_at IS NULL",
            (task_id,),
        ).fetchone()[0]
        if open_sessions > 0:
            print(
                f"Error: Task {task_id} has {open_sessions} open session(s). "
                "Run `tusk session-close <session_id>` first, then retry task-unstart.",
                file=sys.stderr,
            )
            return 2

        # Mirrors tusk-task-reopen.py's trigger-bypass: explicit transaction so
        # DROP TRIGGER and the UPDATE commit atomically, then regen-triggers in
        # the finally block restores the guard even on rollback.
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DROP TRIGGER IF EXISTS validate_status_transition")
            conn.execute(
                "UPDATE tasks SET status = 'To Do', started_at = NULL, "
                "updated_at = datetime('now') WHERE id = ?",
                (task_id,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            regen = subprocess.run(
                ["tusk", "regen-triggers"],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if regen.returncode != 0:
                msg = regen.stderr.strip() or regen.stdout.strip() or "(no output)"
                print(
                    f"Warning: tusk regen-triggers failed (exit {regen.returncode}): {msg}\n"
                    "Run 'tusk regen-triggers' manually to restore the status-transition guard.",
                    file=sys.stderr,
                )

        updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        result = {
            "task": dict(updated),
            "prior_status": "In Progress",
        }
        print(dumps(result))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-unstart <task_id> --force", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
