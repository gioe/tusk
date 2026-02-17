#!/usr/bin/env python3
"""Manage acceptance criteria for tusk tasks.

Called by the tusk wrapper:
    tusk criteria add|list|done|reset ...

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — subcommand + flags
"""

import argparse
import sqlite3
import sys


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def cmd_add(args: argparse.Namespace, db_path: str) -> int:
    conn = get_connection(db_path)

    # Verify task exists
    task = conn.execute("SELECT id FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if not task:
        print(f"Error: Task {args.task_id} not found", file=sys.stderr)
        conn.close()
        return 2

    conn.execute(
        "INSERT INTO acceptance_criteria (task_id, criterion, source) VALUES (?, ?, ?)",
        (args.task_id, args.text, args.source),
    )
    conn.commit()

    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    print(f"Added criterion #{cid} to task #{args.task_id}")
    return 0


def cmd_list(args: argparse.Namespace, db_path: str) -> int:
    conn = get_connection(db_path)

    # Verify task exists
    task = conn.execute(
        "SELECT id, summary FROM tasks WHERE id = ?", (args.task_id,)
    ).fetchone()
    if not task:
        print(f"Error: Task {args.task_id} not found", file=sys.stderr)
        conn.close()
        return 2

    rows = conn.execute(
        "SELECT id, criterion, source, is_completed, created_at "
        "FROM acceptance_criteria WHERE task_id = ? ORDER BY id",
        (args.task_id,),
    ).fetchall()
    conn.close()

    if not rows:
        print(f"No acceptance criteria for task #{args.task_id}: {task['summary']}")
        return 0

    print(f"Acceptance criteria for task #{args.task_id}: {task['summary']}")
    print(f"{'ID':<6} {'Done':<6} {'Source':<14} {'Criterion'}")
    print("-" * 70)
    for r in rows:
        marker = "[x]" if r["is_completed"] else "[ ]"
        print(f"{r['id']:<6} {marker:<6} {r['source']:<14} {r['criterion']}")

    done = sum(1 for r in rows if r["is_completed"])
    print(f"\nProgress: {done}/{len(rows)}")
    return 0


def cmd_done(args: argparse.Namespace, db_path: str) -> int:
    conn = get_connection(db_path)

    row = conn.execute(
        "SELECT id, task_id, criterion, is_completed FROM acceptance_criteria WHERE id = ?",
        (args.criterion_id,),
    ).fetchone()
    if not row:
        print(f"Error: Criterion {args.criterion_id} not found", file=sys.stderr)
        conn.close()
        return 2

    if row["is_completed"]:
        print(f"Criterion #{args.criterion_id} is already completed")
        conn.close()
        return 0

    conn.execute(
        "UPDATE acceptance_criteria SET is_completed = 1, updated_at = datetime('now') WHERE id = ?",
        (args.criterion_id,),
    )
    conn.commit()
    conn.close()
    print(f"Criterion #{args.criterion_id} marked done: {row['criterion']}")
    return 0


def cmd_reset(args: argparse.Namespace, db_path: str) -> int:
    conn = get_connection(db_path)

    row = conn.execute(
        "SELECT id, task_id, criterion, is_completed FROM acceptance_criteria WHERE id = ?",
        (args.criterion_id,),
    ).fetchone()
    if not row:
        print(f"Error: Criterion {args.criterion_id} not found", file=sys.stderr)
        conn.close()
        return 2

    if not row["is_completed"]:
        print(f"Criterion #{args.criterion_id} is already incomplete")
        conn.close()
        return 0

    conn.execute(
        "UPDATE acceptance_criteria SET is_completed = 0, updated_at = datetime('now') WHERE id = ?",
        (args.criterion_id,),
    )
    conn.commit()
    conn.close()
    print(f"Criterion #{args.criterion_id} reset to incomplete: {row['criterion']}")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print("Usage: tusk criteria {add|list|done|reset} ...", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]

    parser = argparse.ArgumentParser(
        prog="tusk criteria",
        description="Manage acceptance criteria for tasks",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # add
    add_p = subparsers.add_parser("add", help="Add a criterion to a task")
    add_p.add_argument("task_id", type=int, help="Task ID")
    add_p.add_argument("text", help="Criterion text")
    add_p.add_argument(
        "--source", default="original",
        choices=["original", "subsumption", "pr_review"],
        help="Source of the criterion (default: original)",
    )

    # list
    list_p = subparsers.add_parser("list", help="List criteria for a task")
    list_p.add_argument("task_id", type=int, help="Task ID")

    # done
    done_p = subparsers.add_parser("done", help="Mark a criterion as completed")
    done_p.add_argument("criterion_id", type=int, help="Criterion ID")

    # reset
    reset_p = subparsers.add_parser("reset", help="Reset a criterion to incomplete")
    reset_p.add_argument("criterion_id", type=int, help="Criterion ID")

    args = parser.parse_args(sys.argv[3:])

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        handlers = {"add": cmd_add, "list": cmd_list, "done": cmd_done, "reset": cmd_reset}
        sys.exit(handlers[args.command](args, db_path))
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
