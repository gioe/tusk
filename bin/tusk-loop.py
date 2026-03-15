#!/usr/bin/env python3
"""Autonomous task loop — continuously works through the backlog.

Called by the tusk wrapper:
    tusk loop [--max-tasks N] [--dry-run] [--on-failure skip|abort]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (accepted for consistency, unused)
    sys.argv[3:] — optional flags

Loop behavior:
  1. Query highest-priority ready task (no incomplete dependencies, no open blockers)
  2. If no task found: stop (empty backlog)
  3. Check if chain head via v_chain_heads view — task in view means it has downstream dependents
  4. If chain head → spawn claude -p /chain <id> [--on-failure <strategy>]
     Else        → spawn claude -p /tusk <id>
  5. On non-zero exit code: stop the loop
  6. Repeat until empty backlog or --max-tasks reached

Flags:
  --max-tasks N          Stop after N tasks regardless of backlog size
  --dry-run              Print what would run without spawning any subprocess
  --on-failure skip|abort  Passed through to each /chain dispatch for unattended runs
"""

import argparse
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

_READY_TASK_SQL = """
SELECT id, summary, priority, priority_score, domain, assignee, complexity
FROM v_ready_tasks
{exclude_clause}
ORDER BY priority_score DESC, id
LIMIT 1
"""

_db_lib = tusk_loader.load("tusk-db-lib")
get_connection = _db_lib.get_connection


def get_next_task(conn: sqlite3.Connection, exclude_ids: set[int] | None = None) -> dict | None:
    """Return the highest-priority ready task, optionally excluding certain IDs."""
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        exclude_clause = f"WHERE id NOT IN ({placeholders})"
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


def is_chain_head(conn: sqlite3.Connection, task_id: int) -> bool:
    """Return True if the task appears in v_chain_heads.

    v_chain_heads selects non-Done tasks that have non-Done downstream dependents,
    no unmet blocks-type upstream deps, and no open external blockers.
    Returns False on any error (falls back to /tusk dispatch).
    """
    try:
        row = conn.execute("SELECT 1 FROM v_chain_heads WHERE id = ?", (task_id,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def spawn_agent(skill: str, task_id: int, on_failure: str | None = None) -> int:
    """Spawn claude -p /<skill> <task_id> [--on-failure <strategy>]. Returns the process exit code."""
    prompt = f"/{skill} {task_id}"
    if skill == "chain" and on_failure:
        prompt += f" --on-failure {on_failure}"
    result = subprocess.run(["claude", "-p", prompt])
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
    parser.add_argument(
        "--on-failure",
        dest="on_failure",
        choices=["skip", "abort"],
        default=None,
        metavar="STRATEGY",
        help="Failure strategy passed through to /chain dispatches: skip (continue to next wave) or abort (stop chain immediately). Has no effect on standalone /tusk dispatches.",
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

            chain_head = is_chain_head(conn, task_id)
            skill = "chain" if chain_head else "tusk"

            if args.dry_run:
                on_failure_suffix = (
                    f" --on-failure {args.on_failure}"
                    if skill == "chain" and args.on_failure
                    else ""
                )
                print(
                    f"[dry-run] Would dispatch: claude -p /{skill} {task_id}{on_failure_suffix}  ({summary})",
                    flush=True,
                )
            else:
                on_failure_suffix = (
                    f" --on-failure {args.on_failure}"
                    if skill == "chain" and args.on_failure
                    else ""
                )
                print(
                    f"Dispatching TASK-{task_id} ({summary}) → claude -p /{skill} {task_id}{on_failure_suffix}",
                    flush=True,
                )
                exit_code = spawn_agent(skill, task_id, on_failure=args.on_failure)
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
