#!/usr/bin/env python3
"""Consolidate task-start setup into a single CLI command.

Called by the tusk wrapper:
    tusk task-start <task_id> [--force] [--agent <name>]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — task_id [--force] [--agent <name>]

Performs all setup steps for beginning work on a task:
  1. Fetch the task (validate it exists and is actionable)
  2. Check for prior progress checkpoints
  3. Reuse an open session or create a new one
  4. Update task status to 'In Progress' (if not already)
  5. Fetch acceptance criteria
  6. Return a JSON blob with task details, progress, criteria, and session_id

--force: bypass the zero-criteria guard (emits a warning but proceeds)
"""

import argparse
import importlib.util
import json
import os
import sqlite3
import sys


def _load_db_lib():
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk-db-lib.py")
    _s = importlib.util.spec_from_file_location("tusk_db_lib", _p)
    _m = importlib.util.module_from_spec(_s)
    _s.loader.exec_module(_m)
    return _m


_db_lib = _load_db_lib()
get_connection = _db_lib.get_connection


def main(argv: list[str]) -> int:
    db_path = argv[0]
    # argv[1] is config_path (unused but kept for dispatch consistency)
    parser = argparse.ArgumentParser(
        prog="tusk task-start",
        description="Begin work on a task",
    )
    parser.add_argument("task_id", type=int, help="Task ID")
    parser.add_argument("--force", action="store_true", help="Bypass zero-criteria guard")
    parser.add_argument("--agent", dest="agent_name", metavar="NAME", help="Agent name")
    args = parser.parse_args(argv[2:])
    task_id = args.task_id
    force = args.force
    agent_name = args.agent_name

    conn = get_connection(db_path)
    try:
        # 1. Fetch the task
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            print(f"Error: Task {task_id} not found", file=sys.stderr)
            return 2

        if task["status"] == "Done":
            print(f"Error: Task {task_id} is already Done", file=sys.stderr)
            return 2

        # 1b. Guard: task must have at least one acceptance criterion
        criteria_count = conn.execute(
            "SELECT COUNT(*) FROM acceptance_criteria WHERE task_id = ? AND is_deferred = 0",
            (task_id,),
        ).fetchone()[0]
        if criteria_count == 0:
            if not force:
                print(
                    f"Error: Task {task_id} has no acceptance criteria. "
                    f"Add at least one before starting work:\n"
                    f"  tusk criteria add {task_id} \"<criterion text>\"",
                    file=sys.stderr,
                )
                return 2
            print(
                f"Warning: Task {task_id} has no acceptance criteria. "
                f"Proceeding anyway due to --force.\n"
                f"  To add criteria: tusk criteria add {task_id} \"<criterion text>\"",
                file=sys.stderr,
            )

        # 1c. Guard: task must not have open external blockers
        open_blockers = conn.execute(
            "SELECT id, description, blocker_type FROM external_blockers "
            "WHERE task_id = ? AND is_resolved = 0",
            (task_id,),
        ).fetchall()
        if open_blockers:
            lines = [f"Error: Task {task_id} has unresolved external blockers:"]
            for b in open_blockers:
                btype = f" [{b['blocker_type']}]" if b["blocker_type"] else ""
                lines.append(f"  • [{b['id']}]{btype} {b['description']}")
            lines.append("Resolve blockers with: tusk blockers resolve <blocker_id>")
            print("\n".join(lines), file=sys.stderr)
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
            # Update agent_name on reused session if --agent was passed
            if agent_name is not None:
                conn.execute(
                    "UPDATE task_sessions SET agent_name = ? WHERE id = ?",
                    (agent_name, session_id),
                )
        else:
            # Create a new session. Under concurrent /chain execution two agents
            # may both read no-open-session and then race to INSERT. The partial
            # UNIQUE index on task_sessions(task_id) WHERE ended_at IS NULL will
            # reject the second INSERT; catch that and fall back to the session
            # the winning agent already created.
            try:
                conn.execute(
                    "INSERT INTO task_sessions (task_id, started_at, agent_name)"
                    " VALUES (?, datetime('now'), ?)",
                    (task_id, agent_name),
                )
                session_id = conn.execute(
                    "SELECT MAX(id) as id FROM task_sessions WHERE task_id = ?",
                    (task_id,),
                ).fetchone()["id"]
            except sqlite3.IntegrityError:
                # Another concurrent agent just opened a session for this task.
                # Reuse it rather than failing.
                print(
                    f"Warning: concurrent session detected for task {task_id}; "
                    f"reusing existing open session.",
                    file=sys.stderr,
                )
                existing = conn.execute(
                    "SELECT id FROM task_sessions WHERE task_id = ? AND ended_at IS NULL "
                    "ORDER BY started_at DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if not existing:
                    print(
                        f"Error: UNIQUE violation but no open session found for task {task_id}.",
                        file=sys.stderr,
                    )
                    return 2
                session_id = existing["id"]
                if agent_name is not None:
                    conn.execute(
                        "UPDATE task_sessions SET agent_name = ? WHERE id = ?",
                        (agent_name, session_id),
                    )

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
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-start <task_id> [--force]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
