#!/usr/bin/env python3
"""List tasks with optional filtering by status, domain, and assignee.

Called by the tusk wrapper:
    tusk task-list [--status <s>] [--domain <d>] [--assignee <a>] [--format text|json] [--all]
                   [--include-shadows] [--bakeoff <id>]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (accepted for dispatch consistency, unused)
    sys.argv[3:] — optional flags

Returns a text table (default) or JSON array of matching tasks.
Defaults to non-Done tasks. --all includes Done tasks.

Shadows (bakeoff_shadow = 1) are excluded by default to keep the normal backlog
view clean. --include-shadows re-includes them; --bakeoff <id> implicitly
includes shadows for that one bakeoff_id (useful for inspecting a bakeoff's
per-model attempt rows).
"""

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection


def print_text_table(rows: list[dict]) -> None:
    if not rows:
        print("No tasks found.")
        return

    columns = ["id", "status", "priority", "complexity", "domain", "assignee", "summary"]
    # Compute column widths
    widths = {col: len(col) for col in columns}
    for row in rows:
        for col in columns:
            val = str(row.get(col) or "")
            if len(val) > widths[col]:
                widths[col] = len(val)

    # Header
    header = "  ".join(col.upper().ljust(widths[col]) for col in columns)
    print(header)
    print("  ".join("-" * widths[col] for col in columns))

    # Rows
    for row in rows:
        line = "  ".join(str(row.get(col) or "").ljust(widths[col]) for col in columns)
        print(line)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "Usage: tusk task-list [--status <s>] [--domain <d>] [--assignee <a>] "
            "[--format text|json] [--all] [--include-shadows] [--bakeoff <id>]",
            file=sys.stderr,
        )
        return 1

    db_path = argv[0]
    # argv[1] is config_path (accepted for dispatch consistency, unused)

    parser = argparse.ArgumentParser(prog="tusk task-list", add_help=False)
    parser.add_argument("--status", default=None, help="Filter by status")
    parser.add_argument("--domain", default=None, help="Filter by domain")
    parser.add_argument("--assignee", default=None, help="Filter by assignee")
    parser.add_argument("--workflow", default=None, help="Filter by workflow")
    parser.add_argument("--format", choices=["text", "json"], default="text", dest="fmt")
    parser.add_argument("--all", action="store_true", dest="all_tasks",
                        help="Include Done tasks (default excludes Done)")
    parser.add_argument("--include-shadows", action="store_true", dest="include_shadows",
                        help="Include bakeoff shadow rows (default: hidden)")
    parser.add_argument("--bakeoff", type=int, default=None,
                        help="Filter to a single bakeoff_id (implicitly includes shadows)")
    parser.add_argument("--help", "-h", action="store_true")
    args, _ = parser.parse_known_args(argv[2:])

    if args.help:
        print(
            "Usage: tusk task-list [--status <s>] [--domain <d>] [--assignee <a>] "
            "[--format text|json] [--all] [--include-shadows] [--bakeoff <id>]"
        )
        print()
        print("Lists tasks from the database.")
        print()
        print("Options:")
        print("  --status            Filter by status — case-sensitive (e.g. 'To Do', 'In Progress', 'Done')")
        print("  --domain            Filter by domain")
        print("  --assignee          Filter by assignee")
        print("  --format            Output format: text (default) or json")
        print("  --all               Include Done tasks (default: only non-Done tasks); ignored when --status is also set")
        print("  --include-shadows   Include bakeoff shadow rows (default: hidden)")
        print("  --bakeoff <id>      Filter to a single bakeoff_id (implicitly includes shadows)")
        return 0

    conditions: list[str] = []
    params: list = []

    # Shadows are hidden by default; --include-shadows or --bakeoff overrides.
    if not args.include_shadows and args.bakeoff is None:
        conditions.append("bakeoff_shadow = 0")

    if args.bakeoff is not None:
        conditions.append("bakeoff_id = ?")
        params.append(args.bakeoff)

    if not args.all_tasks and args.status is None:
        conditions.append("status <> 'Done'")

    if args.status is not None:
        conditions.append("status = ?")
        params.append(args.status)

    if args.domain is not None:
        conditions.append("domain = ?")
        params.append(args.domain)

    if args.assignee is not None:
        conditions.append("assignee = ?")
        params.append(args.assignee)

    if args.workflow is not None:
        conditions.append("workflow = ?")
        params.append(args.workflow)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
SELECT id, summary, status, priority, priority_score, domain, assignee, complexity, task_type, created_at
FROM tasks
{where_clause}
ORDER BY priority_score DESC, id
"""

    conn = get_connection(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    result = [{key: row[key] for key in row.keys()} for row in rows]

    if args.fmt == "json":
        print(dumps(result))
    else:
        print_text_table(result)

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-list [--status <s>] [--domain <d>] [--assignee <a>]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
