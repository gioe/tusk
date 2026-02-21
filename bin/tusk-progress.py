#!/usr/bin/env python3
"""Log a progress checkpoint for a task from the latest git commit.

Called by the tusk wrapper:
    tusk progress <task_id> [--next-steps "..."]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — task_id and optional flags

Gathers commit hash, message, and changed files from the HEAD commit
via git, then INSERTs a row into task_progress.
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


def git(args: list[str]) -> str:
    """Run a git command and return stripped stdout."""
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("Usage: tusk progress <task_id> [--next-steps \"...\"]", file=sys.stderr)
        return 1

    db_path = argv[0]
    # argv[1] is config_path (unused but kept for dispatch consistency)
    remaining = argv[2:]

    # Parse arguments
    task_id_str = None
    next_steps = None

    i = 0
    while i < len(remaining):
        if remaining[i] == "--next-steps":
            if i + 1 >= len(remaining):
                print("Error: --next-steps requires a value", file=sys.stderr)
                return 1
            next_steps = remaining[i + 1]
            i += 2
        elif task_id_str is None:
            task_id_str = remaining[i]
            i += 1
        else:
            print(f"Error: Unexpected argument: {remaining[i]}", file=sys.stderr)
            return 1

    if task_id_str is None:
        print("Usage: tusk progress <task_id> [--next-steps \"...\"]", file=sys.stderr)
        return 1

    try:
        task_id = int(task_id_str)
    except ValueError:
        print(f"Error: Invalid task ID: {task_id_str}", file=sys.stderr)
        return 1

    conn = get_connection(db_path)
    try:
        # Validate task exists
        task = conn.execute("SELECT id, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            print(f"Error: Task {task_id} not found", file=sys.stderr)
            return 2
        if task["status"] == "Done":
            print(f"Error: Task {task_id} is already Done", file=sys.stderr)
            return 2

        # Gather git info from HEAD
        try:
            commit_hash = git(["rev-parse", "--short", "HEAD"])
            commit_message = git(["log", "-1", "--pretty=%s"])
            files_raw = git(["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"])
            files_changed = ", ".join(files_raw.splitlines()) if files_raw else ""
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2

        # Insert progress checkpoint
        conn.execute(
            "INSERT INTO task_progress (task_id, commit_hash, commit_message, files_changed, next_steps) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, commit_hash, commit_message, files_changed, next_steps),
        )
        conn.commit()

        # Print confirmation
        result = {
            "task_id": task_id,
            "commit_hash": commit_hash,
            "commit_message": commit_message,
            "files_changed": files_changed,
            "next_steps": next_steps,
        }
        print(json.dumps(result, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
