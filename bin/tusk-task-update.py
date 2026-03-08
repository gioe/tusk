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
    --deferred            Set is_deferred=1, prefix summary with [Deferred], set expires_at +60d if unset
    --no-deferred         Set is_deferred=0, strip [Deferred] prefix from summary

Only specified fields are updated; unspecified fields are left unchanged.
Always sets updated_at = datetime('now').

Exit codes:
    0 — success (prints JSON with updated task)
    1 — task not found
    2 — validation error or no flags provided
"""

import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

TUSK_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk")


def _load_db_lib():
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk-db-lib.py")
    _s = importlib.util.spec_from_file_location("tusk_db_lib", _p)
    _m = importlib.util.module_from_spec(_s)
    _s.loader.exec_module(_m)
    return _m


_db_lib = _load_db_lib()
get_connection = _db_lib.get_connection
load_config = _db_lib.load_config


def validate_enum(value, valid_values: list, field_name: str) -> str | None:
    """Validate a value against a config list. Returns error message or None."""
    if not valid_values:
        return None  # empty list = no validation
    if value not in valid_values:
        joined = ", ".join(valid_values)
        return f"Invalid {field_name} '{value}'. Valid: {joined}"
    return None


def main(argv: list[str]) -> int:
    db_path = argv[0]
    config_path = argv[1]
    parser = argparse.ArgumentParser(
        prog="tusk task-update",
        description="Update task fields with config validation",
    )
    parser.add_argument("task_id", type=int, help="Task ID")
    parser.add_argument("--summary", default=None, help="Update summary")
    parser.add_argument("--description", default=None, help="Update description")
    parser.add_argument("--priority", default=None, help="Update priority")
    parser.add_argument("--domain", default=None, help="Update domain")
    parser.add_argument("--task-type", default=None, dest="task_type", help="Update task_type")
    parser.add_argument("--assignee", default=None, help="Update assignee")
    parser.add_argument("--complexity", default=None, help="Update complexity")
    deferred_group = parser.add_mutually_exclusive_group()
    deferred_group.add_argument("--deferred", action="store_true", default=False,
                                help="Mark task deferred (+60d expiry, [Deferred] prefix)")
    deferred_group.add_argument("--no-deferred", action="store_true", default=False, dest="no_deferred",
                                help="Clear deferred flag and strip [Deferred] prefix")
    args = parser.parse_args(argv[2:])

    task_id = args.task_id

    # Build updates dict from explicitly-provided optional args
    updates: dict[str, Any] = {}
    for field in ("summary", "description", "priority", "domain", "task_type", "assignee", "complexity"):
        val = getattr(args, field)
        if val is not None:
            updates[field] = val

    # Resolve deferred mode: True = --deferred, False = --no-deferred, None = not specified
    if args.deferred:
        deferred: bool | None = True
    elif args.no_deferred:
        deferred = False
    else:
        deferred = None

    if not updates and deferred is None:
        parser.error("at least one field flag is required")

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

        # Apply --deferred / --no-deferred logic using current task values as base
        if deferred is True:
            current_summary = updates.get("summary", task["summary"])
            if not current_summary.startswith("[Deferred]"):
                updates["summary"] = f"[Deferred] {current_summary}"
            updates["is_deferred"] = 1
            if task["expires_at"] is None and "expires_at" not in updates:
                expires_dt = datetime.now(timezone.utc) + timedelta(days=60)
                updates["expires_at"] = expires_dt.strftime("%Y-%m-%d %H:%M:%S")
        elif deferred is False:
            current_summary = updates.get("summary", task["summary"])
            if current_summary.startswith("[Deferred] "):
                updates["summary"] = current_summary[len("[Deferred] "):]
            elif current_summary.startswith("[Deferred]"):
                updates["summary"] = current_summary[len("[Deferred]"):]
            updates["is_deferred"] = 0
            if "expires_at" not in updates:
                updates["expires_at"] = None

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
            subprocess.run([TUSK_BIN, "wsjf"], capture_output=True)

        # Re-fetch and return updated task
        updated_task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        task_dict = {key: updated_task[key] for key in updated_task.keys()}

        print(json.dumps(task_dict, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-update <task_id> [--priority P] [--domain D] [--summary S]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
