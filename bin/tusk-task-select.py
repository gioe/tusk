#!/usr/bin/env python3
"""Select the top WSJF-ranked ready task, with optional complexity cap.

Called by the tusk wrapper:
    tusk task-select [--max-complexity XS|S|M|L|XL] [--exclude-ids 1,2,3]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (accepted for consistency, unused)
    sys.argv[3:] — optional flags

Returns JSON for the top ready task, or exits with code 1 when none found.
"""

import argparse
import json
import sqlite3
import sys

COMPLEXITY_ORDER = ["XS", "S", "M", "L", "XL"]


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: tusk task-select [--max-complexity XS|S|M|L|XL] [--exclude-ids 1,2,3]", file=sys.stderr)
        return 1

    db_path = argv[0]
    # argv[1] is config_path (accepted for dispatch consistency, unused)

    parser = argparse.ArgumentParser(prog="tusk task-select", add_help=False)
    parser.add_argument("--max-complexity", choices=COMPLEXITY_ORDER, default=None)
    parser.add_argument("--exclude-ids", default=None,
                        help="Comma-separated list of task IDs to exclude from results")
    parser.add_argument("--help", "-h", action="store_true")
    args, _ = parser.parse_known_args(argv[2:])

    if args.help:
        print("Usage: tusk task-select [--max-complexity XS|S|M|L|XL] [--exclude-ids 1,2,3]")
        print()
        print("Returns the top WSJF-ranked ready task as JSON.")
        print("Exit code 1 if no ready tasks exist.")
        print()
        print("Options:")
        print("  --max-complexity  Only return tasks at or below this complexity tier")
        print("  --exclude-ids     Comma-separated task IDs to skip (e.g. for loop delegation)")
        return 0

    exclude_ids: list[int] = []
    if args.exclude_ids:
        try:
            exclude_ids = [int(x.strip()) for x in args.exclude_ids.split(",") if x.strip()]
        except ValueError:
            print("Error: --exclude-ids must be a comma-separated list of integers", file=sys.stderr)
            return 1

    conn = get_connection(db_path)
    try:
        conditions: list[str] = []
        params: list = []

        if args.max_complexity:
            idx = COMPLEXITY_ORDER.index(args.max_complexity)
            allowed = COMPLEXITY_ORDER[: idx + 1]
            placeholders = ",".join("?" * len(allowed))
            conditions.append(f"complexity IN ({placeholders})")
            params.extend(allowed)

        if exclude_ids:
            placeholders = ",".join("?" * len(exclude_ids))
            conditions.append(f"id NOT IN ({placeholders})")
            params.extend(exclude_ids)

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"""
SELECT id, summary, priority, priority_score, domain, assignee, complexity, description
FROM v_ready_tasks
{where_clause}
ORDER BY priority_score DESC, id
LIMIT 1
"""
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()

    if row is None:
        msg = "No ready tasks found"
        if args.max_complexity:
            msg += f" with complexity at or below {args.max_complexity}"
        if exclude_ids:
            msg += f" (excluding {len(exclude_ids)} task ID{'s' if len(exclude_ids) != 1 else ''})"
        print(msg, file=sys.stderr)
        return 1

    result = {
        "id": row["id"],
        "summary": row["summary"],
        "priority": row["priority"],
        "priority_score": row["priority_score"],
        "domain": row["domain"],
        "assignee": row["assignee"],
        "complexity": row["complexity"],
        "description": row["description"],
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
