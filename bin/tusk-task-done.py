#!/usr/bin/env python3
"""Consolidate task closure into a single CLI command.

Called by the tusk wrapper:
    tusk task-done <task_id> --reason <completed|expired|wont_do|duplicate> [--force]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — task_id [--reason <reason>] [--force]

Performs all closure steps for a task:
  1. Validate the task exists and is not already Done
  2. Check for uncompleted acceptance criteria (warns and exits non-zero unless --force)
  2b. Check for completed criteria without a commit hash (warns and exits non-zero unless --force)
  3. Close all open sessions for the task
  4. Update task status to Done with closed_reason
  5. Find and report newly unblocked tasks
  6. Return a JSON blob with task details, sessions closed, and unblocked tasks
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
load_config = _db_lib.load_config


def _find_task_commits(task_id: int) -> list[str]:
    """Return commit hashes referencing [TASK-<task_id>] in git log."""
    try:
        pattern = f"\\[TASK-{task_id}\\]"
        result = subprocess.run(
            ["git", "log", "--format=%H", f"--grep={pattern}"],
            capture_output=True,
            text=True, encoding="utf-8",
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return []


def main(argv: list[str]) -> int:
    db_path = argv[0]
    config_path = argv[1]
    valid_reasons = load_config(config_path).get("closed_reasons", [])
    reason_metavar = "|".join(valid_reasons) if valid_reasons else "completed|expired|wont_do|duplicate"
    parser = argparse.ArgumentParser(
        prog="tusk task-done",
        description="Close a task with a reason",
    )
    parser.add_argument("task_id", type=int, help="Task ID")
    parser.add_argument("--reason", required=True, metavar=reason_metavar, help="Closed reason")
    parser.add_argument("--force", action="store_true", help="Bypass uncompleted criteria check")
    args = parser.parse_args(argv[2:])
    task_id = args.task_id
    reason = args.reason
    force = args.force

    # Validate closed_reason against config
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

        def _print_open_criteria_error() -> None:
            # Task-level counts so the user can tell "these IDs were never marked done"
            # apart from "these IDs are the ones I just completed" — the two sets are
            # disjoint by construction (open_criteria excludes is_completed = 1).
            stats = conn.execute(
                "SELECT "
                " COALESCE(SUM(CASE WHEN is_completed = 1 THEN 1 ELSE 0 END), 0) AS completed, "
                " COALESCE(SUM(CASE WHEN is_deferred = 1 THEN 1 ELSE 0 END), 0) AS deferred, "
                " COUNT(*) AS total "
                "FROM acceptance_criteria WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            completed = stats["completed"] if stats else 0
            deferred = stats["deferred"] if stats else 0
            total = stats["total"] if stats else len(open_criteria)
            deferred_note = f", {deferred} deferred" if deferred else ""
            print(
                f"Error: Task {task_id}: {completed}/{total} criteria done{deferred_note}. "
                f"{len(open_criteria)} not yet marked done:",
                file=sys.stderr,
            )
            for row in open_criteria:
                print(f"  [{row['id']}] {row['criterion']}", file=sys.stderr)
            print(
                "\nThese IDs are the criteria that are still open — distinct from any "
                "criteria already marked done on this task. Mark them done with "
                "`tusk criteria done <id>`, skip with `tusk criteria skip <id> --reason <reason>`, "
                "or re-run with --force to close anyway.",
                file=sys.stderr,
            )

        # Auto-mark only applies to 'completed' closures — wont_do/duplicate/expired
        # tasks may have open criteria intentionally left incomplete.
        if open_criteria and not force and reason == "completed":
            task_commits = _find_task_commits(task_id)
            if task_commits:
                latest_hash = task_commits[0]
                crit_ids = [row["id"] for row in open_criteria]
                placeholders = ",".join("?" * len(crit_ids))
                # Stage the UPDATE but do NOT commit yet — it must be part of the
                # same transaction as the session close and task status update.
                conn.execute(
                    f"UPDATE acceptance_criteria "
                    f"SET is_completed = 1, commit_hash = ?, committed_at = datetime('now'), "
                    f"    updated_at = datetime('now') "
                    f"WHERE id IN ({placeholders})",
                    [latest_hash] + crit_ids,
                )
                open_criteria = []
            else:
                _print_open_criteria_error()
                return 3
        elif open_criteria and not force:
            _print_open_criteria_error()
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
            "UPDATE tasks SET status = 'Done', closed_reason = ?, "
            "closed_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
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

        print(dumps(result))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-done <id> --reason <completed|expired|wont_do|duplicate>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
