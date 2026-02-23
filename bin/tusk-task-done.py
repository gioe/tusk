#!/usr/bin/env python3
"""Consolidate task closure into a single CLI command.

Called by the tusk wrapper:
    tusk task-done <task_id> --reason <closed_reason> [--force]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — task_id --reason <reason> [--force]

Performs all closure steps for a task:
  1. Validate the task exists and is not already Done
  2. Check for uncompleted acceptance criteria (warns and exits non-zero unless --force)
  2b. Check for completed criteria without a commit hash (warns and exits non-zero unless --force)
  3. Close all open sessions for the task
  4. Update task status to Done with closed_reason
  5. Find and report newly unblocked tasks
  6. Return a JSON blob with task details, sessions closed, and unblocked tasks
"""

import json
import sqlite3
import sys


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_closed_reasons(config_path: str) -> list[str]:
    """Load valid closed_reason values from config."""
    try:
        with open(config_path) as f:
            config = json.load(f)
        return config.get("closed_reasons", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return ["completed", "expired", "wont_do", "duplicate"]


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("Usage: tusk task-done <task_id> --reason <closed_reason> [--force]", file=sys.stderr)
        return 1

    db_path = argv[0]
    config_path = argv[1]
    try:
        task_id = int(argv[2])
    except ValueError:
        print(f"Error: Invalid task ID: {argv[2]}", file=sys.stderr)
        return 1

    # Parse --reason and --force flags from remaining args
    remaining = argv[3:]
    reason = None
    force = False
    i = 0
    while i < len(remaining):
        if remaining[i] == "--reason" and i + 1 < len(remaining):
            reason = remaining[i + 1]
            i += 2
        elif remaining[i] == "--force":
            force = True
            i += 1
        else:
            print(f"Error: Unknown argument: {remaining[i]}", file=sys.stderr)
            return 1

    if not reason:
        print("Error: --reason is required", file=sys.stderr)
        return 1

    # Validate closed_reason against config
    valid_reasons = load_closed_reasons(config_path)
    if valid_reasons and reason not in valid_reasons:
        print(f"Error: Invalid closed_reason '{reason}'. Valid: {', '.join(valid_reasons)}", file=sys.stderr)
        return 1

    conn = get_connection(db_path)
    try:
        # 1. Fetch and validate the task
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            print(f"Error: Task {task_id} not found", file=sys.stderr)
            return 2

        if task["status"] == "Done":
            print(f"Error: Task {task_id} is already Done", file=sys.stderr)
            return 2

        # 2. Check for uncompleted acceptance criteria (deferred criteria do not block closure)
        open_criteria = conn.execute(
            "SELECT id, criterion FROM acceptance_criteria "
            "WHERE task_id = ? AND is_completed = 0 AND is_deferred = 0",
            (task_id,),
        ).fetchall()

        if open_criteria and not force:
            print(f"Error: Task {task_id} has {len(open_criteria)} uncompleted acceptance criteria:", file=sys.stderr)
            for row in open_criteria:
                print(f"  [{row['id']}] {row['criterion']}", file=sys.stderr)
            print("\nUse --force to close anyway.", file=sys.stderr)
            return 3

        # 2b. Check for completed criteria without a commit hash (only for completed tasks)
        # Skipped for wont_do/duplicate/expired — commit traceability only matters for completed work
        if reason == "completed":
            uncommitted_criteria = conn.execute(
                "SELECT id, criterion FROM acceptance_criteria "
                "WHERE task_id = ? AND is_completed = 1 AND commit_hash IS NULL",
                (task_id,),
            ).fetchall()

            if uncommitted_criteria:
                label = "Warning" if force else "Error"
                print(
                    f"{label}: Task {task_id} has {len(uncommitted_criteria)} completed "
                    f"criteria without a commit hash:",
                    file=sys.stderr,
                )
                for row in uncommitted_criteria:
                    print(f"  [{row['id']}] {row['criterion']}", file=sys.stderr)
                if not force:
                    print(
                        "\nCriteria must be backed by a commit before closing. "
                        "Use --force to close anyway (e.g. for non-git environments "
                        "or criteria completed before commit tracking was introduced).",
                        file=sys.stderr,
                    )
                    return 3

        # 3. Close all open sessions
        cursor = conn.execute(
            "UPDATE task_sessions "
            "SET ended_at = datetime('now'), "
            "    duration_seconds = CAST((julianday(datetime('now')) - julianday(started_at)) * 86400 AS INTEGER), "
            "    lines_added = COALESCE(lines_added, 0), "
            "    lines_removed = COALESCE(lines_removed, 0) "
            "WHERE task_id = ? AND ended_at IS NULL",
            (task_id,),
        )
        sessions_closed = cursor.rowcount

        # 4. Update task status to Done
        conn.execute(
            "UPDATE tasks SET status = 'Done', closed_reason = ?, updated_at = datetime('now') WHERE id = ?",
            (reason, task_id),
        )

        conn.commit()

        # 5. Find newly unblocked tasks
        unblocked_rows = conn.execute(
            "SELECT t.id, t.summary, t.priority, t.priority_score "
            "FROM tasks t "
            "JOIN task_dependencies d ON t.id = d.task_id "
            "WHERE d.depends_on_id = ? "
            "  AND t.status = 'To Do' "
            "  AND NOT EXISTS ( "
            "    SELECT 1 FROM task_dependencies d2 "
            "    JOIN tasks blocker ON d2.depends_on_id = blocker.id "
            "    WHERE d2.task_id = t.id AND blocker.status <> 'Done' "
            "  ) "
            "  AND NOT EXISTS ( "
            "    SELECT 1 FROM external_blockers eb "
            "    WHERE eb.task_id = t.id AND eb.is_resolved = 0 "
            "  )",
            (task_id,),
        ).fetchall()

        # 6. Build and return JSON result
        # Re-fetch task to get updated values
        updated_task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        task_dict = {key: updated_task[key] for key in updated_task.keys()}
        unblocked_list = [{key: row[key] for key in row.keys()} for row in unblocked_rows]

        result = {
            "task": task_dict,
            "sessions_closed": sessions_closed,
            "unblocked_tasks": unblocked_list,
        }

        print(json.dumps(result, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
