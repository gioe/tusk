#!/usr/bin/env python3
"""Return config and backlog in a single JSON object.

Called by the tusk wrapper:
    tusk setup

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
"""

import json
import sqlite3
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: tusk setup", file=sys.stderr)
        return 1

    db_path = argv[0]
    config_path = argv[1]

    # Load config
    try:
        with open(config_path) as f:
            config = json.load(f)
    except FileNotFoundError:
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in config: {e}", file=sys.stderr)
        return 2

    # Query backlog
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            rows = conn.execute(
                "SELECT id, summary, status, priority, domain, assignee, complexity, task_type, priority_score "
                "FROM tasks WHERE status <> 'Done' ORDER BY priority_score DESC, id"
            ).fetchall()
            backlog = [dict(row) for row in rows]
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"Error: Database query failed: {e}", file=sys.stderr)
        return 2

    result = {
        "config": config,
        "backlog": backlog,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
