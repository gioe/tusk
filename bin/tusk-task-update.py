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
    --not-before <ts>     Update not_before, or empty string to clear

Only specified fields are updated; unspecified fields are left unchanged.
Always sets updated_at = datetime('now').

Exit codes:
    0 — success (prints JSON with updated task)
    1 — task not found
    2 — validation error or no flags provided
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

TUSK_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk")

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_task_insert = tusk_loader.load("tusk-task-insert")
_git_helpers = tusk_loader.load("tusk-git-helpers")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
load_config = _db_lib.load_config
validate_enum = _db_lib.validate_enum
reject_shell_metacharacters = _git_helpers.reject_shell_metacharacters


def _rederive_auto_scope(
    conn: sqlite3.Connection,
    task_id: int,
    config_path: str,
) -> None:
    if conn.execute(
        "SELECT 1 FROM task_scope WHERE task_id = ? AND source = 'unbounded' LIMIT 1",
        (task_id,),
    ).fetchone():
        conn.execute(
            "DELETE FROM task_scope WHERE task_id = ? AND source = 'auto_derived'",
            (task_id,),
        )
        return

    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return

    explicit_rows = conn.execute(
        "SELECT pattern FROM task_scope WHERE task_id = ? AND source <> 'auto_derived'",
        (task_id,),
    ).fetchall()
    explicit_patterns = {row["pattern"] for row in explicit_rows}

    text_blocks = [task["summary"] or "", task["description"] or ""]
    criteria = conn.execute(
        "SELECT criterion, verification_spec FROM acceptance_criteria WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    for criterion in criteria:
        text_blocks.append(criterion["criterion"] or "")
        text_blocks.append(criterion["verification_spec"] or "")

    repo_root = _task_insert._repo_root(config_path)
    task_type = task["task_type"] if "task_type" in task.keys() else None
    seen_auto: set[str] = set()
    derived: list[str] = []
    requires_unit_tests = any(
        _task_insert._UNIT_TEST_REQUIREMENT_RE.search(block or "")
        for block in text_blocks
    )
    for text in text_blocks:
        for path in _task_insert._auto_scope_candidates(
            text,
            repo_root=repo_root,
            task_type=task_type,
            requires_unit_tests=requires_unit_tests,
        ):
            if _task_insert.is_prose_identifier_path(path, repo_root):
                continue
            resolved = _task_insert._resolve_auto_derived_scope_pattern(repo_root, path)
            if resolved in explicit_patterns or resolved in seen_auto:
                continue
            seen_auto.add(resolved)
            derived.append(resolved)

    conn.execute(
        "DELETE FROM task_scope WHERE task_id = ? AND source = 'auto_derived'",
        (task_id,),
    )
    for pattern in derived:
        conn.execute(
            "INSERT INTO task_scope (task_id, pattern, source) "
            "VALUES (?, ?, 'auto_derived')",
            (task_id, pattern),
        )


def main(argv: list[str]) -> int:
    db_path = argv[0]
    config_path = argv[1]
    parser = argparse.ArgumentParser(allow_abbrev=False,
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
    parser.add_argument("--workflow", default=None, help="Update workflow (empty string clears to NULL)")
    parser.add_argument(
        "--not-before",
        default=None,
        dest="not_before",
        metavar="TIMESTAMP",
        help=(
            "Update not_before; accepts ISO or +Nm/+Nh/+Nd/+Nw. "
            "Pass an empty string to clear to NULL."
        ),
    )
    args = parser.parse_args(argv[2:])

    task_id = args.task_id

    # Build updates dict from explicitly-provided optional args
    updates: dict[str, Any] = {}
    for field in ("summary", "description", "priority", "domain", "task_type", "assignee", "complexity"):
        val = getattr(args, field)
        if val is not None:
            updates[field] = val

    # --workflow: empty string clears to NULL, non-empty sets the value
    if args.workflow is not None:
        updates["workflow"] = None if args.workflow == "" else args.workflow

    # --not-before is explicit-only. Description phrase detection is deferred:
    # task-update is non-interactive, so prose heuristics should not silently
    # change scheduling without a dedicated warning/prompt surface.
    if args.not_before is not None:
        if args.not_before == "":
            updates["not_before"] = None
        else:
            try:
                updates["not_before"] = _task_insert._parse_not_before(args.not_before)
            except argparse.ArgumentTypeError as exc:
                parser.error(str(exc))

    if not updates:
        parser.error("at least one field flag is required")

    # Reject shell-substitution metacharacters in the text fields before any DB
    # write (issue #1106 — extends the issue #881 commit-message guard). zsh/bash
    # expand `, $(...), ${...}, and bare $IDENT before tusk sees the argv, even
    # inside double quotes, silently corrupting stored content. task-update has
    # no file-input escape hatch, so the only fix is to rewrite the value.
    for field, subject in (("summary", "task summary"), ("description", "task description")):
        if field in updates:
            ok, diagnostic = reject_shell_metacharacters(updates[field], subject=subject)
            if not ok:
                print(diagnostic, file=sys.stderr)
                return 1

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

    if "workflow" in updates and updates["workflow"] is not None:
        err = validate_enum(updates["workflow"], config.get("workflows", []), "workflow")
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
            if "summary" in updates or "description" in updates:
                _rederive_auto_scope(conn, task_id, config_path)
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

        print(dumps(task_dict))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-update <task_id> [--priority P] [--domain D] [--summary S]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
