#!/usr/bin/env python3
"""Downstream sub-DAG operations scoped to one or more head tasks.

Called by the tusk wrapper:
    tusk chain scope|frontier|status <head_task_id> [<head_task_id2> ...]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — subcommand + flags
"""

import argparse
import json
import logging
import sqlite3
import sys
from collections import deque

log = logging.getLogger(__name__)


def get_connection(db_path: str) -> sqlite3.Connection:
    """Get database connection with foreign keys enabled."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def task_exists(conn: sqlite3.Connection, task_id: int) -> bool:
    """Check if a task exists."""
    result = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return result is not None


def bfs_downstream(conn: sqlite3.Connection, head_id: int) -> list[tuple[int, int]]:
    """BFS from head following dependents direction (depends_on_id -> task_id).

    Returns list of (task_id, depth) pairs for all tasks in the downstream
    sub-DAG, including the head task at depth 0.
    """
    visited = {head_id: 0}
    queue = deque([(head_id, 0)])
    result = [(head_id, 0)]

    while queue:
        current_id, depth = queue.popleft()
        dependents = conn.execute(
            "SELECT task_id FROM task_dependencies WHERE depends_on_id = ?",
            (current_id,),
        ).fetchall()
        for row in dependents:
            dep_id = row["task_id"]
            if dep_id not in visited:
                visited[dep_id] = depth + 1
                queue.append((dep_id, depth + 1))
                result.append((dep_id, depth + 1))

    return result


def bfs_downstream_union(conn: sqlite3.Connection, head_ids: list[int]) -> list[tuple[int, int]]:
    """Run BFS from all heads and return union of downstream tasks with min depth.

    All heads are treated as depth 0. Tasks reachable from multiple heads
    get the minimum depth across all paths.
    """
    depth_map: dict[int, int] = {}

    for head_id in head_ids:
        for tid, depth in bfs_downstream(conn, head_id):
            if tid not in depth_map or depth < depth_map[tid]:
                depth_map[tid] = depth

    return list(depth_map.items())


def validate_multi_head(conn: sqlite3.Connection, head_ids: list[int]) -> None:
    """Validate that multiple heads share at least one common downstream task.

    Exits with error if all heads have completely disjoint sub-DAGs, since
    multi-head chains are only meaningful when heads converge on shared dependents.
    """
    if len(head_ids) <= 1:
        return

    head_set = set(head_ids)
    non_head_downstream_sets = []

    for hid in head_ids:
        ds = bfs_downstream(conn, hid)
        non_head_ds = {tid for tid, _ in ds if tid not in head_set}
        non_head_downstream_sets.append(non_head_ds)

    # Accept if any two heads share a common downstream task
    for i in range(len(non_head_downstream_sets)):
        for j in range(i + 1, len(non_head_downstream_sets)):
            if non_head_downstream_sets[i] & non_head_downstream_sets[j]:
                return

    heads_str = " ".join(str(h) for h in head_ids)
    print(
        f"Error: tasks {heads_str} have no common downstream tasks — "
        "multi-head chains are only useful when heads converge on shared dependents. "
        "Run separate /chain or /tusk invocations instead.",
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_scope(conn: sqlite3.Connection, head_ids: list[int]):
    """Return JSON with head tasks, all downstream tasks, depths, and completion counts."""
    downstream = bfs_downstream_union(conn, head_ids)
    task_ids = [tid for tid, _ in downstream]
    depth_map = {tid: d for tid, d in downstream}

    # Fetch task details for all tasks in scope
    placeholders = ",".join("?" * len(task_ids))
    rows = conn.execute(
        f"SELECT id, summary, status, priority, complexity, assignee, description FROM tasks WHERE id IN ({placeholders})",
        task_ids,
    ).fetchall()

    tasks = []
    done_count = 0
    for row in rows:
        tid = row["id"]
        if row["status"] == "Done":
            done_count += 1
        tasks.append({
            "id": tid,
            "summary": row["summary"],
            "description": row["description"],
            "status": row["status"],
            "priority": row["priority"],
            "complexity": row["complexity"],
            "assignee": row["assignee"],
            "depth": depth_map[tid],
        })

    # Sort by depth then id for stable output
    tasks.sort(key=lambda t: (t["depth"], t["id"]))

    output = {
        "head_task_ids": head_ids,
        "total_tasks": len(tasks),
        "completed": done_count,
        "remaining": len(tasks) - done_count,
        "tasks": tasks,
    }
    print(json.dumps(output, indent=2))


def cmd_frontier(conn: sqlite3.Connection, head_ids: list[int]):
    """Return tasks within union scope that are To Do with all deps met."""
    downstream = bfs_downstream_union(conn, head_ids)
    task_ids = [tid for tid, _ in downstream]

    if not task_ids:
        print(json.dumps({"head_task_ids": head_ids, "frontier": []}))
        return

    placeholders = ",".join("?" * len(task_ids))
    # Tasks in scope that are To Do and have no incomplete blocking dependencies
    ready = conn.execute(
        f"""
        SELECT t.id, t.summary, t.priority, t.complexity
        FROM tasks t
        WHERE t.id IN ({placeholders})
          AND t.status = 'To Do'
          AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on_id = blocker.id
            WHERE d.task_id = t.id AND d.relationship_type = 'blocks' AND blocker.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM external_blockers eb
            WHERE eb.task_id = t.id AND eb.is_resolved = 0
          )
        ORDER BY t.id
        """,
        task_ids,
    ).fetchall()

    frontier = [
        {
            "id": row["id"],
            "summary": row["summary"],
            "priority": row["priority"],
            "complexity": row["complexity"],
        }
        for row in ready
    ]

    print(json.dumps({"head_task_ids": head_ids, "frontier": frontier}, indent=2))


def cmd_frontier_check(conn: sqlite3.Connection, head_ids: list[int]):
    """Return status and frontier in a single call for loop-termination detection.

    Outputs JSON: {"status": "complete|stuck|continue", "frontier": [...]}
      - complete:  all tasks in the subgraph are Done
      - stuck:     some tasks are not Done but no ready tasks remain in the frontier
      - continue:  frontier has at least one ready task
    """
    downstream = bfs_downstream_union(conn, head_ids)
    task_ids = [tid for tid, _ in downstream]

    if not task_ids:
        print(json.dumps({"status": "complete", "frontier": []}))
        return

    placeholders = ",".join("?" * len(task_ids))
    ready = conn.execute(
        f"""
        SELECT t.id, t.summary, t.priority, t.complexity
        FROM tasks t
        WHERE t.id IN ({placeholders})
          AND t.status = 'To Do'
          AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on_id = blocker.id
            WHERE d.task_id = t.id AND d.relationship_type = 'blocks' AND blocker.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM external_blockers eb
            WHERE eb.task_id = t.id AND eb.is_resolved = 0
          )
        ORDER BY t.id
        """,
        task_ids,
    ).fetchall()

    frontier = [
        {
            "id": row["id"],
            "summary": row["summary"],
            "priority": row["priority"],
            "complexity": row["complexity"],
        }
        for row in ready
    ]

    if frontier:
        status = "continue"
    else:
        not_done = conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE id IN ({placeholders}) AND status <> 'Done'",
            task_ids,
        ).fetchone()[0]
        status = "complete" if not_done == 0 else "stuck"

    print(json.dumps({"status": status, "frontier": frontier}, indent=2))


def cmd_status(conn: sqlite3.Connection, head_ids: list[int]):
    """Print a human-readable progress summary for the downstream sub-DAG."""
    downstream = bfs_downstream_union(conn, head_ids)
    task_ids = [tid for tid, _ in downstream]

    placeholders = ",".join("?" * len(task_ids))
    rows = conn.execute(
        f"SELECT id, summary, status FROM tasks WHERE id IN ({placeholders})",
        task_ids,
    ).fetchall()

    head_summaries: dict[int, str] = {}
    by_status: dict[str, list] = {"Done": [], "In Progress": [], "To Do": []}
    for row in rows:
        if row["id"] in set(head_ids):
            head_summaries[row["id"]] = row["summary"]
        status = row["status"]
        if status in by_status:
            by_status[status].append(row)

    total = len(rows)
    done = len(by_status["Done"])
    in_progress = len(by_status["In Progress"])
    todo = len(by_status["To Do"])
    pct = round(done / total * 100) if total > 0 else 0

    if len(head_ids) == 1:
        hid = head_ids[0]
        print(f"Chain status for Task {hid}: {head_summaries.get(hid, '')}")
    else:
        heads_str = ", ".join(str(h) for h in head_ids)
        print(f"Chain status for Tasks {heads_str}")
        for hid in head_ids:
            print(f"  Head {hid}: {head_summaries.get(hid, '')}")
    print("=" * 60)
    print(f"Progress: {done}/{total} tasks completed ({pct}%)")
    print(f"  Done:        {done}")
    print(f"  In Progress: {in_progress}")
    print(f"  To Do:       {todo}")

    if in_progress > 0:
        print(f"\nIn Progress:")
        for row in by_status["In Progress"]:
            print(f"  - [{row['id']}] {row['summary']}")

    if todo > 0:
        print(f"\nTo Do:")
        for row in by_status["To Do"]:
            print(f"  - [{row['id']}] {row['summary']}")


def main():
    if len(sys.argv) < 3:
        print("Usage: tusk chain <subcommand> <head_task_id> [<head_task_id2> ...]", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    # sys.argv[2] is config path (unused by this script, accepted for consistency)

    parser = argparse.ArgumentParser(
        description="Downstream sub-DAG operations scoped to one or more head tasks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  tusk chain scope 42              # JSON: all tasks downstream of 42 with depths
  tusk chain scope 42 43           # JSON: union of sub-DAGs rooted at 42 and 43
  tusk chain frontier 42           # JSON: ready tasks within 42's sub-DAG
  tusk chain frontier 42 43        # JSON: ready tasks in union of 42+43 sub-DAGs
  tusk chain frontier-check 42     # JSON: {status, frontier} for loop termination
  tusk chain frontier-check 42 43  # JSON: {status, frontier} for multi-head chain
  tusk chain status 42             # Human-readable progress summary
  tusk chain status 42 43          # Human-readable progress for multi-head chain
        """,
    )
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug output")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # scope command
    scope_parser = subparsers.add_parser("scope", help="List all tasks in the downstream sub-DAG")
    scope_parser.add_argument("head_task_ids", type=int, nargs="+", help="Head task ID(s)")

    # frontier command
    frontier_parser = subparsers.add_parser("frontier", help="List ready tasks within scope")
    frontier_parser.add_argument("head_task_ids", type=int, nargs="+", help="Head task ID(s)")

    # frontier-check command
    frontier_check_parser = subparsers.add_parser(
        "frontier-check",
        help="Return {status, frontier} JSON for loop-termination detection",
    )
    frontier_check_parser.add_argument("head_task_ids", type=int, nargs="+", help="Head task ID(s)")

    # status command
    status_parser = subparsers.add_parser("status", help="Show progress summary")
    status_parser.add_argument("head_task_ids", type=int, nargs="+", help="Head task ID(s)")

    args = parser.parse_args(sys.argv[3:])

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="[debug] %(message)s",
        stream=sys.stderr,
    )
    log.debug("DB path: %s", db_path)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    conn = get_connection(db_path)
    try:
        for tid in args.head_task_ids:
            if not task_exists(conn, tid):
                print(f"Error: Task {tid} does not exist", file=sys.stderr)
                sys.exit(1)

        # For multi-head calls, validate that heads share at least one common downstream task
        if len(args.head_task_ids) > 1:
            validate_multi_head(conn, args.head_task_ids)

        if args.command == "scope":
            cmd_scope(conn, args.head_task_ids)
        elif args.command == "frontier":
            cmd_frontier(conn, args.head_task_ids)
        elif args.command == "frontier-check":
            cmd_frontier_check(conn, args.head_task_ids)
        elif args.command == "status":
            cmd_status(conn, args.head_task_ids)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
