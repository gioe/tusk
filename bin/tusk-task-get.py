#!/usr/bin/env python3
"""Read-only fetch of a single task bundle.

Called by the tusk wrapper:
    tusk task-get <task_id>

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (unused)
    sys.argv[3] — task_id (integer or TASK-NNN form)

Returns JSON with task row, acceptance_criteria array, and task_progress array.
Does not modify any state.
"""

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py and tusk-json-lib.py

_db_lib = tusk_loader.load("tusk-db-lib")
get_connection = _db_lib.get_connection
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps


def _task_id_type(value: str) -> int:
    """Accept plain integer or TASK-NNN form."""
    v = value
    if v.upper().startswith("TASK-"):
        v = v[5:]
    try:
        return int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid task ID: {value}")


def main(argv: list[str]) -> int:
    db_path = argv[0]
    # argv[1] is config_path (unused)
    parser = argparse.ArgumentParser(
        prog="tusk task-get",
        description="Fetch a single task bundle",
    )
    parser.add_argument("task_id", type=_task_id_type, help="Task ID (integer or TASK-NNN form)")
    args = parser.parse_args(argv[2:])
    task_id = args.task_id

    conn = get_connection(db_path)
    try:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            print(f"Error: Task {task_id} not found", file=sys.stderr)
            return 1

        criteria_rows = conn.execute(
            "SELECT id, task_id, criterion, source, is_completed, "
            "criterion_type, verification_spec, created_at, updated_at "
            "FROM acceptance_criteria WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()

        progress_rows = conn.execute(
            "SELECT * FROM task_progress WHERE task_id = ? ORDER BY created_at DESC",
            (task_id,),
        ).fetchall()

        result = {
            "task": {key: task[key] for key in task.keys()},
            "acceptance_criteria": [{key: row[key] for key in row.keys()} for row in criteria_rows],
            "task_progress": [{key: row[key] for key in row.keys()} for row in progress_rows],
        }

        print(dumps(result))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-get <task_id>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
