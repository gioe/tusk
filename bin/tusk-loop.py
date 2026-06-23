#!/usr/bin/env python3
"""Autonomous task loop — continuously works through the backlog.

Called by the tusk wrapper:
    tusk loop [--max-tasks N] [--dry-run] [--on-failure skip|abort]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (accepted for consistency, unused)
    sys.argv[3:] — optional flags

Loop behavior (drain-then-propose):
  1. Query highest-priority ready task (no incomplete dependencies, no open blockers)
  2. If no task found: run `tusk propose-work` and surface the ranked candidates
     to the operator (node `propose_on_empty`), then stop. Proposals are
     surfaced only — never auto-created — preserving the human gate on task
     origination.
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


def decide_dispatch(task: dict | None, chain_head: bool) -> dict:
    """Pure decision: given the next ready task (or None) and whether it is a chain
    head, return the dispatch decision as a dict with a ``node`` field.

    Nodes:
      - ``propose_on_empty`` — no ready task; surface `tusk propose-work` candidates
        to the operator instead of stopping silently. Proposals are surfaced only,
        never auto-created — the human gate on task origination is preserved.
      - ``chain``            — dispatch the chain head via /chain.
      - ``tusk``             — dispatch the standalone task via /tusk.
    """
    if task is None:
        return {"node": "propose_on_empty", "skill": None, "task_id": None}
    if chain_head:
        return {"node": "chain", "skill": "chain", "task_id": task["id"]}
    return {"node": "tusk", "skill": "tusk", "task_id": task["id"]}


def propose_work() -> int:
    """Run `tusk propose-work` and stream its ranked candidates to the operator.

    Surfaces origination candidates only — never inserts tasks. Returns the
    process exit code (non-fatal: failures degrade to an empty surface).
    """
    result = subprocess.run(
        ["tusk", "propose-work"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    candidates = (result.stdout or "").strip()
    if result.returncode == 0 and candidates and candidates != "[]":
        print(
            "Backlog drained — ranked work proposals (review and create via "
            "/create-task; nothing is auto-created):",
            flush=True,
        )
        print(candidates, flush=True)
    elif result.returncode == 0:
        print("Backlog drained — no work proposals surfaced.", flush=True)
    else:
        stderr = (result.stderr or "").strip()
        print(
            f"Backlog drained — `tusk propose-work` exited {result.returncode}"
            + (f": {stderr}" if stderr else "")
            + " — no proposals surfaced.",
            file=sys.stderr,
            flush=True,
        )
    return result.returncode


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

    parser = argparse.ArgumentParser(allow_abbrev=False,
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

            chain_head = is_chain_head(conn, task["id"]) if task is not None else False
            decision = decide_dispatch(task, chain_head)

            if decision["node"] == "propose_on_empty":
                # Drain-then-propose: instead of stopping silently, surface ranked
                # work proposals for the operator to act on. Nothing is auto-created.
                print("Backlog empty — surfacing work proposals.", flush=True)
                if not args.dry_run:
                    propose_work()
                else:
                    print("[dry-run] Would run: tusk propose-work", flush=True)
                break

            task_id = task["id"]
            summary = task["summary"]
            skill = decision["skill"]

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
