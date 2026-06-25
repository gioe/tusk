#!/usr/bin/env python3
"""Log a progress checkpoint for a task from the latest git commit.

Called by the tusk wrapper:
    tusk progress <task_id> [--note "..."] [--next-steps "..."]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — task_id and optional flags

Gathers commit hash, message, and changed files from the HEAD commit
via git, then INSERTs a row into task_progress.
"""

import json
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py, tusk-json-lib.py, tusk-git-helpers.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_git_helpers = tusk_loader.load("tusk-git-helpers")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
reject_shell_metacharacters = _git_helpers.reject_shell_metacharacters


def git(args: list[str]) -> str:
    """Run a git command and return stripped stdout."""
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def commit_message_belongs_to_task(commit_message: str, task_id: int) -> bool:
    return f"[TASK-{task_id}]" in commit_message


def main(argv: list[str]) -> int:
    usage = 'Usage: tusk progress <task_id> [--note "..."] [--next-steps "..."]'
    if len(argv) < 3:
        print(usage, file=sys.stderr)
        return 1

    db_path = argv[0]
    # argv[1] is config_path (unused but kept for dispatch consistency)
    remaining = argv[2:]

    # Parse arguments
    task_id_str = None
    note = None
    next_steps = None

    i = 0
    while i < len(remaining):
        if remaining[i] == "--note":
            if i + 1 >= len(remaining):
                print("Error: --note requires a value", file=sys.stderr)
                return 1
            note = remaining[i + 1]
            i += 2
        elif remaining[i] == "--next-steps":
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
        print(usage, file=sys.stderr)
        return 1

    try:
        task_id = int(task_id_str)
    except ValueError:
        print(f"Error: Invalid task ID: {task_id_str}", file=sys.stderr)
        return 1

    # Reject shell-substitution metacharacters in the free-text fields before any
    # DB write (issue #1107 — extends the issue #881/#1106 guard). zsh/bash expand
    # `, $(...), ${...}, and bare $IDENT before tusk sees the argv, even inside
    # double quotes, silently corrupting the stored note/next_steps.
    for value, subject in ((note, "progress note"), (next_steps, "progress next-steps")):
        if value is not None:
            ok, diagnostic = reject_shell_metacharacters(value, subject=subject)
            if not ok:
                print(diagnostic, file=sys.stderr)
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
            if commit_message_belongs_to_task(commit_message, task_id):
                files_raw = git(["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"])
                files_changed = ", ".join(files_raw.splitlines()) if files_raw else ""
            else:
                commit_hash = None
                commit_message = None
                files_changed = None
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2

        # Insert progress checkpoint
        conn.execute(
            "INSERT INTO task_progress (task_id, commit_hash, commit_message, files_changed, note, next_steps) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, commit_hash, commit_message, files_changed, note, next_steps),
        )
        conn.commit()

        # Print confirmation
        result = {
            "task_id": task_id,
            "commit_hash": commit_hash,
            "commit_message": commit_message,
            "files_changed": files_changed,
            "note": note,
            "next_steps": next_steps,
        }
        print(dumps(result))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print('Use: tusk progress <task_id> [--note "..."] [--next-steps "..."]', file=sys.stderr)
        sys.exit(1)
    # Retry the whole command (a fresh connection per attempt) on transient
    # "database is locked" contention under parallel worktree sessions (#1143).
    sys.exit(_db_lib.retry_on_locked(lambda: main(sys.argv[1:]), label="progress"))
