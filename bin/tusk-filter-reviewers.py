#!/usr/bin/env python3
"""Filter reviewers from config by task domain and return matching names as JSON.

Called by the tusk wrapper:
    tusk filter-reviewers --task-id <id>

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — flags: --task-id <id>

Domain-matching logic:
    - A reviewer with an empty or absent `domains` array → always included.
    - A reviewer with a non-empty `domains` array → included only if the task's
      domain appears in that array.
    - If the task has no domain (NULL) → only reviewers with an empty or absent
      `domains` array are included.

Output:
    JSON array of matching reviewer name strings, e.g. ["general", "security"]
"""

import json
import os
import sqlite3
import sys


def get_task_domain(db_path: str, task_id: int) -> str | None:
    """Return the domain of the given task, or None if not set."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT domain FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            print(f"Error: task {task_id} not found", file=sys.stderr)
            sys.exit(1)
        return row["domain"]
    finally:
        conn.close()


def filter_reviewers(reviewers: list, task_domain: str | None) -> list[str]:
    """Apply three-branch domain-matching logic and return matching reviewer names."""
    matched = []
    for reviewer in reviewers:
        domains = reviewer.get("domains") or []
        if not domains:
            # Empty or absent domains array → always include
            matched.append(reviewer["name"])
        elif task_domain and task_domain in domains:
            # Non-empty domains array → include only if task domain matches
            matched.append(reviewer["name"])
        # else: non-empty domains but task_domain is None or not in list → skip
    return matched


def main(argv: list) -> int:
    if len(argv) < 2:
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk filter-reviewers --task-id <id>", file=sys.stderr)
        return 1

    db_path = argv[0]
    config_path = argv[1]
    args = argv[2:]

    # Parse --task-id <id>
    task_id = None
    i = 0
    while i < len(args):
        if args[i] == "--task-id" and i + 1 < len(args):
            try:
                task_id = int(args[i + 1])
            except ValueError:
                print(f"Error: --task-id must be an integer, got: {args[i + 1]}", file=sys.stderr)
                return 1
            i += 2
        else:
            print(f"Error: Unknown argument: {args[i]}", file=sys.stderr)
            return 1

    if task_id is None:
        print("Error: --task-id <id> is required", file=sys.stderr)
        return 1

    # Load config
    try:
        with open(config_path) as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error: could not load config: {e}", file=sys.stderr)
        return 1

    reviewers = config.get("review", {}).get("reviewers", [])

    # Get task domain from DB
    task_domain = get_task_domain(db_path, task_id)

    # Apply filter logic
    matched = filter_reviewers(reviewers, task_domain)

    print(json.dumps(matched))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk filter-reviewers --task-id <id>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
