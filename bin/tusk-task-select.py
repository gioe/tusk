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
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py, tusk-json-lib.py, tusk-rank-lib.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_rank_lib = tusk_loader.load("tusk-rank-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
select_top_ready_task = _rank_lib.select_top_ready_task
empty_backlog_message = _rank_lib.empty_backlog_message
COMPLEXITY_ORDER = _rank_lib.COMPLEXITY_ORDER


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
        row = select_top_ready_task(
            conn,
            max_complexity=args.max_complexity,
            exclude_ids=exclude_ids,
        )

        warn_rows: list = []
        if row is not None:
            text = (row["description"] or "") + " " + (row["summary"] or "")
            referenced_ids = list({
                int(m.group(1))
                for m in re.finditer(r'\bTASK-(\d+)\b', text, re.IGNORECASE)
                if int(m.group(1)) != row["id"]
            })
            if referenced_ids:
                placeholders = ",".join("?" * len(referenced_ids))
                warn_rows = conn.execute(
                    f"SELECT id, summary FROM tasks WHERE id IN ({placeholders}) AND status = 'To Do'",
                    referenced_ids,
                ).fetchall()
    finally:
        conn.close()

    if row is None:
        print(
            empty_backlog_message(
                max_complexity=args.max_complexity,
                exclude_ids=exclude_ids,
            ),
            file=sys.stderr,
        )
        return 1

    if warn_rows:
        print("Warning: selected task references unfinished prerequisite tasks:", file=sys.stderr)
        for wr in warn_rows:
            print(f"  TASK-{wr['id']}: {wr['summary']}", file=sys.stderr)

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
    print(dumps(result))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-select [--max-complexity XS|S|M|L|XL]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
