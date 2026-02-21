#!/usr/bin/env python3
"""tusk task-reopen: Reset a stuck In Progress (or Done) task back to To Do."""

import json
import sqlite3
import subprocess
import sys


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("Usage: tusk task-reopen <task_id> [--force]", file=sys.stderr)
        return 1

    db_path = argv[0]
    # argv[1] is config_path (unused but kept for dispatch consistency)
    try:
        task_id = int(argv[2])
    except (ValueError, IndexError):
        print(f"Error: Invalid task ID: {argv[2]}", file=sys.stderr)
        return 1

    remaining = argv[3:]
    force = False
    i = 0
    while i < len(remaining):
        if remaining[i] == "--force":
            force = True
            i += 1
        else:
            print(f"Error: Unknown argument: {remaining[i]}", file=sys.stderr)
            return 1

    if not force:
        print(
            f"This will reset task {task_id} back to 'To Do', clearing any closed_reason.\n"
            "Re-run with --force to confirm:\n"
            f"  tusk task-reopen {task_id} --force",
            file=sys.stderr,
        )
        return 1

    conn = get_connection(db_path)
    try:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            print(f"Error: Task {task_id} not found.", file=sys.stderr)
            return 2

        if task["status"] == "To Do":
            print(
                f"Error: Task {task_id} is already 'To Do' — nothing to reopen.",
                file=sys.stderr,
            )
            return 2

        if task["status"] not in ("In Progress", "Done"):
            print(
                f"Error: Task {task_id} has unexpected status '{task['status']}'. "
                "Only 'In Progress' and 'Done' tasks can be reopened.",
                file=sys.stderr,
            )
            return 2

        prior_status = task["status"]

        # Use explicit transaction control (isolation_level=None = autocommit) so that
        # DROP TRIGGER and the two UPDATEs all commit atomically. Without this, Python's
        # sqlite3 module auto-commits DDL before DML, leaving a window where the trigger
        # is absent but the status has not yet been reset.
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Close any open sessions, computing duration_seconds to match tusk-task-done.py
            sessions_closed = conn.execute(
                "UPDATE task_sessions "
                "SET ended_at = datetime('now'), "
                "    duration_seconds = CAST((julianday(datetime('now')) - julianday(started_at)) * 86400 AS INTEGER) "
                "WHERE task_id = ? AND ended_at IS NULL",
                (task_id,),
            ).rowcount

            # Drop the status-transition trigger so we can move the status backwards.
            # The trigger is recreated via `tusk regen-triggers` after COMMIT.
            conn.execute("DROP TRIGGER IF EXISTS validate_status_transition")

            conn.execute(
                "UPDATE tasks SET status = 'To Do', closed_reason = NULL, "
                "updated_at = datetime('now') WHERE id = ?",
                (task_id,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            # Regenerate the status-transition trigger. Always runs (even on rollback)
            # so the DB is never permanently missing the guard. Check the return code
            # and surface any failure visibly rather than swallowing it.
            regen = subprocess.run(
                ["tusk", "regen-triggers"],
                capture_output=True,
                text=True,
            )
            if regen.returncode != 0:
                msg = regen.stderr.strip() or regen.stdout.strip() or "(no output)"
                print(
                    f"Warning: tusk regen-triggers failed (exit {regen.returncode}): {msg}\n"
                    "Run 'tusk regen-triggers' manually to restore the status-transition guard.",
                    file=sys.stderr,
                )

        updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        task_dict = dict(updated)

        result = {
            "task": task_dict,
            "prior_status": prior_status,
            "sessions_closed": sessions_closed,
        }
        print(json.dumps(result, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
