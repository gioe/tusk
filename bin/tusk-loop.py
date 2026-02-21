#!/usr/bin/env python3
"""Autonomous task loop — continuously works through the backlog.

Called by the tusk wrapper:
    tusk loop [--max-tasks N] [--dry-run]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (accepted for consistency, unused)
    sys.argv[3:] — optional flags

Loop behavior:
  1. Query highest-priority ready task (no incomplete dependencies, no open blockers)
  2. If no task found: stop (empty backlog)
  3. Check if chain head via tusk chain scope — total_tasks > 1 means dependents exist
  4. If chain head → spawn claude -p /chain <id>
     Else        → spawn claude -p /next-task <id>
  5. On non-zero exit code: stop the loop
  6. Repeat until empty backlog or --max-tasks reached

Flags:
  --max-tasks N   Stop after N tasks regardless of backlog size
  --dry-run       Print what would run without spawning any subprocess
"""

import argparse
import json
import sqlite3
import subprocess
import sys


_READY_TASK_SQL = """
SELECT t.id, t.summary, t.priority, t.priority_score, t.domain, t.assignee, t.complexity
FROM tasks t
WHERE t.status = 'To Do'
  AND NOT EXISTS (
    SELECT 1 FROM task_dependencies d
    JOIN tasks blocker ON d.depends_on_id = blocker.id
    WHERE d.task_id = t.id AND blocker.status <> 'Done'
  )
  AND NOT EXISTS (
    SELECT 1 FROM external_blockers eb
    WHERE eb.task_id = t.id AND eb.is_resolved = 0
  )
{exclude_clause}
ORDER BY t.priority_score DESC, t.id
LIMIT 1
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_next_task(conn: sqlite3.Connection, exclude_ids: set[int] | None = None) -> dict | None:
    """Return the highest-priority ready task, optionally excluding certain IDs."""
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        exclude_clause = f"AND t.id NOT IN ({placeholders})"
        sql = _READY_TASK_SQL.format(exclude_clause=exclude_clause)
        row = conn.execute(sql, list(exclude_ids)).fetchone()
    else:
        sql = _READY_TASK_SQL.format(exclude_clause="")
        row = conn.execute(sql).fetchone()

    if row is None:
        return None
    return {
        "id": row["id"],
        "summary": row["summary"],
        "priority": row["priority"],
        "priority_score": row["priority_score"],
        "domain": row["domain"],
        "assignee": row["assignee"],
        "complexity": row["complexity"],
    }


def is_chain_head(task_id: int) -> bool:
    """Return True if the task has non-Done downstream dependents (use /chain).

    Calls tusk chain scope and checks total_tasks > 1.
    Returns False on any error (falls back to /next-task dispatch).
    """
    try:
        result = subprocess.run(
            ["tusk", "chain", "scope", str(task_id)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
        return data.get("total_tasks", 1) > 1
    except (subprocess.SubprocessError, json.JSONDecodeError, KeyError):
        return False


def spawn_agent(skill: str, task_id: int) -> int:
    """Spawn claude -p /<skill> <task_id>. Returns the process exit code."""
    result = subprocess.run(["claude", "-p", f"/{skill} {task_id}"])
    return result.returncode


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: tusk loop [--max-tasks N] [--dry-run]", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    # sys.argv[2] is config path — accepted for CLI consistency, not used here

    parser = argparse.ArgumentParser(
        description="Autonomous task loop — works through the backlog until empty",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  tusk loop                   # Run until backlog is empty
  tusk loop --max-tasks 3     # Stop after 3 tasks
  tusk loop --dry-run         # Show what would run without executing
        """,
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=0,
        metavar="N",
        help="Stop after N tasks (default: 0 = unlimited)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would run without spawning any subprocess",
    )
    args = parser.parse_args(sys.argv[3:])

    if args.max_tasks < 0:
        print("Error: --max-tasks must be a positive integer", file=sys.stderr)
        sys.exit(1)

    conn = get_connection(db_path)
    tasks_run = 0
    # Track dispatched IDs to prevent re-dispatching the same task if an agent
    # exits 0 but leaves the task in 'To Do' (silent failure).
    dispatched_ids: set[int] = set()

    print("tusk loop started", flush=True)

    try:
        while True:
            task = get_next_task(conn, exclude_ids=dispatched_ids if dispatched_ids else None)

            if task is None:
                print("Backlog empty — loop complete.", flush=True)
                break

            task_id = task["id"]
            summary = task["summary"]

            chain_head = is_chain_head(task_id)
            skill = "chain" if chain_head else "next-task"

            if args.dry_run:
                print(
                    f"[dry-run] Would dispatch: claude -p /{skill} {task_id}  ({summary})",
                    flush=True,
                )
            else:
                print(
                    f"Dispatching TASK-{task_id} ({summary}) → claude -p /{skill} {task_id}",
                    flush=True,
                )
                exit_code = spawn_agent(skill, task_id)
                if exit_code != 0:
                    print(
                        f"Agent exited with code {exit_code} for TASK-{task_id} — stopping loop.",
                        file=sys.stderr,
                        flush=True,
                    )
                    sys.exit(exit_code)

            dispatched_ids.add(task_id)
            tasks_run += 1
            if args.max_tasks > 0 and tasks_run >= args.max_tasks:
                print(f"Reached --max-tasks {args.max_tasks} — stopping loop.", flush=True)
                break
    finally:
        conn.close()

    print(f"tusk loop finished. Tasks processed: {tasks_run}", flush=True)


if __name__ == "__main__":
    main()
