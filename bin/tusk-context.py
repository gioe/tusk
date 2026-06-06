#!/usr/bin/env python3
"""Manage task context items used by task-brief context hydration.

Called by the tusk wrapper:
    tusk context add <task_id> --type <type> --content <text> [flags]
    tusk context list <task_id> [--type <type>] [--status <status>|all] [--format json|text]
    tusk context resolve <context_item_id>
    tusk context supersede <context_item_id>

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (reserved)
    sys.argv[3:] — subcommand + flags

Output: compact JSON by default. `list --format text` emits a simple table.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py and tusk-json-lib.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection


ITEM_TYPES = ("memory", "assumption", "question", "risk", "decision", "entry_point")
STATUSES = ("active", "resolved", "superseded")
SOURCES = ("manual", "create_task", "task_progress", "review", "retro", "agent_handoff")


def _parse_task_id(raw: str) -> int:
    s = (raw or "").strip()
    if s.upper().startswith("TASK-"):
        s = s[5:]
    if not re.fullmatch(r"[0-9]+", s):
        raise ValueError(f"invalid task_id: {raw!r}")
    return int(s)


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def _ensure_task_exists(conn: sqlite3.Connection, task_id: int) -> None:
    row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"task {task_id} not found")


def _ensure_objective_exists(conn: sqlite3.Connection, objective_id: int) -> None:
    row = conn.execute("SELECT id FROM objectives WHERE id = ?", (objective_id,)).fetchone()
    if row is None:
        raise ValueError(f"objective {objective_id} not found")


def _fetch_context_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, task_id, objective_id, item_type, content, status, source, "
        "       created_at, updated_at, resolved_at "
        "  FROM task_context_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"context item {item_id} not found")
    return row


def _format_text(rows: list[dict]) -> str:
    if not rows:
        return "No context items found."
    headers = ("id", "type", "status", "source", "content")
    widths = {
        "id": max(2, max(len(str(r["id"])) for r in rows)),
        "type": max(4, max(len(str(r["item_type"])) for r in rows)),
        "status": max(6, max(len(str(r["status"])) for r in rows)),
        "source": max(6, max(len(str(r["source"])) for r in rows)),
        "content": 7,
    }
    lines = [
        f"{headers[0]:>{widths['id']}}  "
        f"{headers[1]:<{widths['type']}}  "
        f"{headers[2]:<{widths['status']}}  "
        f"{headers[3]:<{widths['source']}}  "
        f"{headers[4]}"
    ]
    lines.append(
        f"{'-' * widths['id']}  "
        f"{'-' * widths['type']}  "
        f"{'-' * widths['status']}  "
        f"{'-' * widths['source']}  "
        f"{'-' * widths['content']}"
    )
    for row in rows:
        content = " ".join((row["content"] or "").split())
        lines.append(
            f"{row['id']:>{widths['id']}}  "
            f"{row['item_type']:<{widths['type']}}  "
            f"{row['status']:<{widths['status']}}  "
            f"{row['source']:<{widths['source']}}  "
            f"{content}"
        )
    return "\n".join(lines)


def cmd_add(args: argparse.Namespace, conn: sqlite3.Connection) -> dict:
    task_id = _parse_task_id(args.task_id)
    content = (args.content or "").strip()
    if not content:
        raise ValueError("--content must not be empty")
    _ensure_task_exists(conn, task_id)
    if args.objective_id is not None:
        _ensure_objective_exists(conn, args.objective_id)

    cursor = conn.execute(
        "INSERT INTO task_context_items "
        "  (task_id, objective_id, item_type, content, source) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, args.objective_id, args.item_type, content, args.source),
    )
    conn.commit()
    return _row_to_dict(_fetch_context_item(conn, int(cursor.lastrowid)))


def cmd_list(args: argparse.Namespace, conn: sqlite3.Connection) -> list[dict]:
    task_id = _parse_task_id(args.task_id)
    _ensure_task_exists(conn, task_id)

    where = ["task_id = ?"]
    params: list = [task_id]
    if args.item_type is not None:
        where.append("item_type = ?")
        params.append(args.item_type)
    if args.status != "all":
        where.append("status = ?")
        params.append(args.status)

    rows = conn.execute(
        "SELECT id, task_id, objective_id, item_type, content, status, source, "
        "       created_at, updated_at, resolved_at "
        "  FROM task_context_items "
        f" WHERE {' AND '.join(where)} "
        " ORDER BY item_type, created_at, id",
        params,
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _update_status(
    args: argparse.Namespace,
    conn: sqlite3.Connection,
    *,
    status: str,
) -> dict:
    item_id = int(args.context_item_id)
    _fetch_context_item(conn, item_id)
    conn.execute(
        "UPDATE task_context_items "
        "   SET status = ?, updated_at = datetime('now'), resolved_at = datetime('now') "
        " WHERE id = ?",
        (status, item_id),
    )
    conn.commit()
    return _row_to_dict(_fetch_context_item(conn, item_id))


def main(argv: list[str]) -> int:
    db_path = argv[0]
    # argv[1] is config_path — reserved
    parser = argparse.ArgumentParser(
        prog="tusk context",
        description="Manage typed task context items for durable handoff.",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    add = sub.add_parser("add", help="Create one task context item.")
    add.add_argument("task_id", help="Task ID, e.g. 42 or TASK-42.")
    add.add_argument("--type", dest="item_type", choices=ITEM_TYPES, required=True)
    add.add_argument("--content", required=True)
    add.add_argument("--source", choices=SOURCES, default="manual")
    add.add_argument("--objective-id", type=int, default=None)

    ls = sub.add_parser("list", help="List context items for a task.")
    ls.add_argument("task_id", help="Task ID, e.g. 42 or TASK-42.")
    ls.add_argument("--type", dest="item_type", choices=ITEM_TYPES, default=None)
    ls.add_argument("--status", choices=(*STATUSES, "all"), default="active")
    ls.add_argument("--format", choices=("json", "text"), default="json")

    resolve = sub.add_parser("resolve", help="Mark a context item resolved.")
    resolve.add_argument("context_item_id", type=int)

    supersede = sub.add_parser("supersede", help="Mark a context item superseded.")
    supersede.add_argument("context_item_id", type=int)

    args = parser.parse_args(argv[2:])

    conn = get_connection(db_path)
    try:
        try:
            if args.mode == "add":
                print(dumps(cmd_add(args, conn)))
                return 0
            if args.mode == "list":
                rows = cmd_list(args, conn)
                if args.format == "text":
                    print(_format_text(rows))
                else:
                    print(dumps(rows))
                return 0
            if args.mode == "resolve":
                print(dumps(_update_status(args, conn, status="resolved")))
                return 0
            if args.mode == "supersede":
                print(dumps(_update_status(args, conn, status="superseded")))
                return 0
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except sqlite3.IntegrityError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    finally:
        conn.close()

    return 2


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk context add <task_id> --type <type> --content <text>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
