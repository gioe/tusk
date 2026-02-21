#!/usr/bin/env python3
"""Update task fields with config validation.

Called by the tusk wrapper:
    tusk task-update <task_id> [flags...]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — task_id and optional flags

Flags:
    --summary <text>      Update summary
    --description <text>  Update description
    --priority <p>        Update priority
    --domain <d>          Update domain
    --task-type <t>       Update task_type
    --assignee <a>        Update assignee
    --complexity <c>      Update complexity
    --github-pr <url>     Update github_pr

Only specified fields are updated; unspecified fields are left unchanged.
Always sets updated_at = datetime('now').

Exit codes:
    0 — success (prints JSON with updated task)
    1 — task not found
    2 — validation error or no flags provided
"""

import json
import sqlite3
import subprocess
import sys


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return json.load(f)


def validate_enum(value, valid_values: list, field_name: str) -> str | None:
    """Validate a value against a config list. Returns error message or None."""
    if not valid_values:
        return None  # empty list = no validation
    if value not in valid_values:
        joined = ", ".join(valid_values)
        return f"Invalid {field_name} '{value}'. Valid: {joined}"
    return None


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(
            "Usage: tusk task-update <task_id> [--priority P] [--domain D] "
            "[--task-type T] [--assignee A] [--complexity C] "
            "[--summary S] [--description D] [--github-pr URL]",
            file=sys.stderr,
        )
        return 2

    db_path = argv[0]
    config_path = argv[1]

    try:
        task_id = int(argv[2])
    except ValueError:
        print(f"Error: Invalid task ID: {argv[2]}", file=sys.stderr)
        return 2

    # Parse flags
    remaining = argv[3:]
    updates: dict[str, str] = {}

    flag_map = {
        "--summary": "summary",
        "--description": "description",
        "--priority": "priority",
        "--domain": "domain",
        "--task-type": "task_type",
        "--assignee": "assignee",
        "--complexity": "complexity",
        "--github-pr": "github_pr",
    }

    i = 0
    while i < len(remaining):
        arg = remaining[i]
        if arg in flag_map:
            if i + 1 >= len(remaining):
                print(f"Error: {arg} requires a value", file=sys.stderr)
                return 2
            updates[flag_map[arg]] = remaining[i + 1]
            i += 2
        else:
            print(f"Error: Unknown argument: {arg}", file=sys.stderr)
            return 2

    if not updates:
        print("Error: At least one field flag is required", file=sys.stderr)
        return 2

    # Validate enum fields against config
    config = load_config(config_path)
    errors = []

    if "priority" in updates:
        err = validate_enum(updates["priority"], config.get("priorities", []), "priority")
        if err:
            errors.append(err)

    if "domain" in updates:
        err = validate_enum(updates["domain"], config.get("domains", []), "domain")
        if err:
            errors.append(err)

    if "task_type" in updates:
        err = validate_enum(updates["task_type"], config.get("task_types", []), "task_type")
        if err:
            errors.append(err)

    if "complexity" in updates:
        err = validate_enum(updates["complexity"], config.get("complexity", []), "complexity")
        if err:
            errors.append(err)

    if "assignee" in updates:
        agents = config.get("agents", {})
        if agents:
            valid_agents = list(agents.keys())
            err = validate_enum(updates["assignee"], valid_agents, "assignee")
            if err:
                errors.append(err)

    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        return 2

    # Verify task exists
    conn = get_connection(db_path)
    try:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            print(f"Error: Task {task_id} not found", file=sys.stderr)
            return 1

        # Build dynamic SET clause
        set_parts = []
        params = []
        for col, val in updates.items():
            set_parts.append(f"{col} = ?")
            params.append(val)
        set_parts.append("updated_at = datetime('now')")
        params.append(task_id)

        sql = f"UPDATE tasks SET {', '.join(set_parts)} WHERE id = ?"

        try:
            conn.execute(sql, params)
            conn.commit()
        except sqlite3.Error as e:
            conn.rollback()
            print(f"Database error: {e}", file=sys.stderr)
            return 2

        # Re-score WSJF if priority or complexity changed (inputs to the formula)
        if "priority" in updates or "complexity" in updates:
            subprocess.run(["tusk", "wsjf"], capture_output=True)

        # Re-fetch and return updated task
        updated_task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        task_dict = {key: updated_task[key] for key in updated_task.keys()}

        print(json.dumps(task_dict, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
