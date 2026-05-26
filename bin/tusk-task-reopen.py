#!/usr/bin/env python3
"""tusk task-reopen: Reset a stuck In Progress (or Done) task back to To Do."""

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
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
status_transition_trigger_bypassed = _db_lib.status_transition_trigger_bypassed


def main(argv: list[str]) -> int:
    db_path = argv[0]
    # argv[1] is config_path (unused but kept for dispatch consistency)
    parser = argparse.ArgumentParser(
        prog="tusk task-reopen",
        description="Reset a task back to To Do",
    )
    parser.add_argument("task_id", type=int, help="Task ID")
    parser.add_argument("--force", action="store_true", help="Confirm the reset")
    args = parser.parse_args(argv[2:])
    task_id = args.task_id
    force = args.force

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

        # The status-transition trigger forbids backwards moves; drop it for
        # the duration of the UPDATEs via the shared helper. Session-close
        # and status-reset land in one atomic transaction so callers never
        # see the intermediate "trigger missing + status not yet reverted"
        # state. Snapshot/restore + regen-triggers choreography lives in
        # tusk-db-lib so a third caller can't re-introduce the bug (#844).
        sessions_closed = 0
        with status_transition_trigger_bypassed(conn):
            sessions_closed = conn.execute(
                "UPDATE task_sessions "
                "SET ended_at = datetime('now'), "
                "    duration_seconds = CAST((julianday(datetime('now')) - julianday(started_at)) * 86400 AS INTEGER) "
                "WHERE task_id = ? AND ended_at IS NULL",
                (task_id,),
            ).rowcount
            conn.execute(
                "UPDATE tasks SET status = 'To Do', closed_reason = NULL, "
                "updated_at = datetime('now') WHERE id = ?",
                (task_id,),
            )

        updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        task_dict = dict(updated)

        result = {
            "task": task_dict,
            "prior_status": prior_status,
            "sessions_closed": sessions_closed,
        }
        print(dumps(result))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-reopen <task_id> --force", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
