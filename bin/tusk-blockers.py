#!/usr/bin/env python3
"""Manage external blockers for tusk tasks.

Called by the tusk wrapper:
    tusk blockers add|list|resolve|remove|blocked|all ...

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — subcommand + flags
"""

import argparse
import json
import sqlite3
import sys


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_blocker_types(config_path: str) -> list[str]:
    """Load valid blocker_type values from config."""
    try:
        with open(config_path) as f:
            config = json.load(f)
        return config.get("blocker_types", [])
    except (OSError, json.JSONDecodeError):
        return []


def cmd_add(args: argparse.Namespace, db_path: str, config_path: str) -> int:
    conn = get_connection(db_path)
    try:
        task = conn.execute("SELECT id FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        if args.type:
            valid_types = load_blocker_types(config_path)
            if valid_types and args.type not in valid_types:
                print(f"Error: Invalid blocker_type '{args.type}'. Valid: {', '.join(valid_types)}", file=sys.stderr)
                return 2

        conn.execute(
            "INSERT INTO external_blockers (task_id, description, blocker_type) VALUES (?, ?, ?)",
            (args.task_id, args.description, args.type),
        )
        conn.commit()

        bid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        type_str = f" [{args.type}]" if args.type else ""
        print(f"Added blocker #{bid} to task #{args.task_id}{type_str}: {args.description}")
        return 0
    finally:
        conn.close()


def cmd_list(args: argparse.Namespace, db_path: str) -> int:
    conn = get_connection(db_path)
    try:
        task = conn.execute(
            "SELECT id, summary FROM tasks WHERE id = ?", (args.task_id,)
        ).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        rows = conn.execute(
            "SELECT id, description, blocker_type, is_resolved, resolved_at, created_at "
            "FROM external_blockers WHERE task_id = ? ORDER BY id",
            (args.task_id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print(f"No blockers for task #{args.task_id}: {task['summary']}")
        return 0

    print(f"Blockers for task #{args.task_id}: {task['summary']}")
    print(f"{'ID':<6} {'Status':<10} {'Type':<12} {'Description'}")
    print("-" * 70)
    for r in rows:
        marker = "[resolved]" if r["is_resolved"] else "[open]"
        btype = r["blocker_type"] or "-"
        print(f"{r['id']:<6} {marker:<10} {btype:<12} {r['description']}")

    resolved = sum(1 for r in rows if r["is_resolved"])
    print(f"\nResolved: {resolved}/{len(rows)}")
    return 0


def cmd_resolve(args: argparse.Namespace, db_path: str) -> int:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT id, task_id, description, is_resolved FROM external_blockers WHERE id = ?",
            (args.blocker_id,),
        ).fetchone()
        if not row:
            print(f"Error: Blocker {args.blocker_id} not found", file=sys.stderr)
            return 2

        if row["is_resolved"]:
            print(f"Blocker #{args.blocker_id} is already resolved")
            return 0

        conn.execute(
            "UPDATE external_blockers SET is_resolved = 1, resolved_at = datetime('now') WHERE id = ?",
            (args.blocker_id,),
        )
        conn.commit()
        print(f"Blocker #{args.blocker_id} resolved: {row['description']}")
        return 0
    finally:
        conn.close()


def cmd_remove(args: argparse.Namespace, db_path: str) -> int:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT id, description FROM external_blockers WHERE id = ?",
            (args.blocker_id,),
        ).fetchone()
        if not row:
            print(f"Error: Blocker {args.blocker_id} not found", file=sys.stderr)
            return 2

        conn.execute("DELETE FROM external_blockers WHERE id = ?", (args.blocker_id,))
        conn.commit()
        print(f"Removed blocker #{args.blocker_id}: {row['description']}")
        return 0
    finally:
        conn.close()


def cmd_blocked(db_path: str) -> int:
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT t.id, t.summary, t.status, t.priority,
                COUNT(eb.id) as blocker_count
            FROM tasks t
            JOIN external_blockers eb ON eb.task_id = t.id
            WHERE eb.is_resolved = 0
            GROUP BY t.id
            ORDER BY t.priority_score DESC, t.id
        """).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No tasks with unresolved blockers")
        return 0

    print("Tasks with unresolved external blockers")
    print(f"{'ID':<6} {'Status':<14} {'Priority':<10} {'Blockers':<10} {'Summary'}")
    print("-" * 70)
    for r in rows:
        print(f"{r['id']:<6} {r['status']:<14} {r['priority'] or 'N/A':<10} {r['blocker_count']:<10} {r['summary']}")
    return 0


def cmd_all(db_path: str) -> int:
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT eb.id, eb.task_id, t.summary as task_summary,
                eb.description, eb.blocker_type, eb.is_resolved, eb.resolved_at, eb.created_at
            FROM external_blockers eb
            JOIN tasks t ON eb.task_id = t.id
            ORDER BY eb.is_resolved, eb.task_id, eb.id
        """).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No blockers defined")
        return 0

    print("All external blockers")
    print(f"{'ID':<6} {'Task':<6} {'Status':<10} {'Type':<12} {'Description':<30} {'Task Summary'}")
    print("-" * 90)
    for r in rows:
        marker = "resolved" if r["is_resolved"] else "open"
        btype = r["blocker_type"] or "-"
        desc = r["description"][:28] if len(r["description"]) > 28 else r["description"]
        summary = r["task_summary"][:30]
        print(f"{r['id']:<6} {r['task_id']:<6} {marker:<10} {btype:<12} {desc:<30} {summary}")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print("Usage: tusk blockers {add|list|resolve|remove|blocked|all} ...", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    config_path = sys.argv[2]

    parser = argparse.ArgumentParser(
        prog="tusk blockers",
        description="Manage external blockers for tasks",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # add
    add_p = subparsers.add_parser("add", help="Add an external blocker to a task")
    add_p.add_argument("task_id", type=int, help="Task ID")
    add_p.add_argument("description", help="Blocker description")
    add_p.add_argument(
        "--type", default=None,
        help="Blocker type (e.g., data, approval, infra, external)",
    )

    # list
    list_p = subparsers.add_parser("list", help="List blockers for a task")
    list_p.add_argument("task_id", type=int, help="Task ID")

    # resolve
    resolve_p = subparsers.add_parser("resolve", help="Mark a blocker as resolved")
    resolve_p.add_argument("blocker_id", type=int, help="Blocker ID")

    # remove
    remove_p = subparsers.add_parser("remove", help="Delete a blocker")
    remove_p.add_argument("blocker_id", type=int, help="Blocker ID")

    # blocked
    subparsers.add_parser("blocked", help="List tasks with unresolved blockers")

    # all
    subparsers.add_parser("all", help="List all blockers")

    args = parser.parse_args(sys.argv[3:])

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "add":
            sys.exit(cmd_add(args, db_path, config_path))
        elif args.command == "list":
            sys.exit(cmd_list(args, db_path))
        elif args.command == "resolve":
            sys.exit(cmd_resolve(args, db_path))
        elif args.command == "remove":
            sys.exit(cmd_remove(args, db_path))
        elif args.command == "blocked":
            sys.exit(cmd_blocked(db_path))
        elif args.command == "all":
            sys.exit(cmd_all(db_path))
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
