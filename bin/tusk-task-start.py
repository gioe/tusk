#!/usr/bin/env python3
"""Consolidate task-start setup into a single CLI command.

Called by the tusk wrapper:
    tusk task-start <task_id>

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — task_id

Performs all setup steps for beginning work on a task:
  1. Fetch the task (validate it exists and is actionable)
  2. Check for prior progress checkpoints
  3. Reuse an open session or create a new one
  4. Update task status to 'In Progress' (if not already)
  5. Fetch acceptance criteria
  6. Return a JSON blob with task details, progress, criteria, and session_id
"""

import json
import sqlite3
import sys


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("Usage: tusk task-start <task_id>", file=sys.stderr)
        return 1

    db_path = argv[0]
    # argv[1] is config_path (unused but kept for dispatch consistency)
    try:
        task_id = int(argv[2])
    except ValueError:
        print(f"Error: Invalid task ID: {argv[2]}", file=sys.stderr)
        return 1

    conn = get_connection(db_path)

    # 1. Fetch the task
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        print(f"Error: Task {task_id} not found", file=sys.stderr)
        conn.close()
        return 2

    if task["status"] == "Done":
        print(f"Error: Task {task_id} is already Done", file=sys.stderr)
        conn.close()
        return 2

    # 2. Check for prior progress
    progress_rows = conn.execute(
        "SELECT * FROM task_progress WHERE task_id = ? ORDER BY created_at DESC",
        (task_id,),
    ).fetchall()

    # 3. Check for an open session to reuse
    open_session = conn.execute(
        "SELECT id FROM task_sessions WHERE task_id = ? AND ended_at IS NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()

    if open_session:
        session_id = open_session["id"]
    else:
        # Create a new session
        conn.execute(
            "INSERT INTO task_sessions (task_id, started_at) VALUES (?, datetime('now'))",
            (task_id,),
        )
        session_id = conn.execute(
            "SELECT MAX(id) as id FROM task_sessions WHERE task_id = ?",
            (task_id,),
        ).fetchone()["id"]

    # 4. Update status to In Progress (if not already)
    if task["status"] != "In Progress":
        conn.execute(
            "UPDATE tasks SET status = 'In Progress', updated_at = datetime('now') WHERE id = ?",
            (task_id,),
        )

    conn.commit()

    # 5. Fetch acceptance criteria
    criteria_rows = conn.execute(
        "SELECT id, task_id, criterion, source, is_completed, "
        "criterion_type, verification_spec, created_at, updated_at "
        "FROM acceptance_criteria WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()

    # 6. Build and return JSON result
    task_dict = {key: task[key] for key in task.keys()}
    task_dict["status"] = "In Progress"
    progress_list = [{key: row[key] for key in row.keys()} for row in progress_rows]
    criteria_list = [{key: row[key] for key in row.keys()} for row in criteria_rows]

    result = {
        "task": task_dict,
        "progress": progress_list,
        "criteria": criteria_list,
        "session_id": session_id,
    }

    print(json.dumps(result, indent=2))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
