#!/usr/bin/env python3
"""Check for existing deliverables when a task has criteria completed but no commits.

Called by the tusk wrapper:
    tusk check-deliverables <task_id>

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3] — task_id (integer or TASK-NNN prefix form)

Output JSON:
    {
        "commits_found": bool,
        "files_found": bool,
        "files": ["path/that/exists", ...],
        "default_branch_commits": ["sha1", ...],
        "default_branch_commit_files": ["path/changed/by/default/commits", ...],
        "recommendation": "commits_found" | "merged_not_closed" | "merged_not_closed_low_confidence" | "mark_done" | "criteria_complete_no_commits" | "implement_fresh"
    }

Recommendations:
    "commits_found"                       — commits referencing this task exist on a non-default branch — normal path
    "merged_not_closed"                   — commits already on the default branch and their diff overlaps with task scope (or there is no scope signal to compare) — skip implementation, go straight to finalize
    "merged_not_closed_low_confidence"    — commits exist on the default branch but their diff doesn't overlap with files referenced in the task or with files modified on any feature branch — likely a [TASK-N] prefix-match false positive — verify before acting
    "mark_done"                           — no commits, but deliverable files found on disk — mark criteria done and merge
    "criteria_complete_no_commits"        — every non-deferred acceptance criterion is marked is_completed=1 but there are no [TASK-N] commits anywhere and no deliverable files on disk — salvage / converged-work / speculative-mark signal — investigate before re-implementing
    "implement_fresh"                     — no commits, no files found, and at least one criterion is still incomplete (or the task has no criteria) — proceed with implementation

Exit codes:
    0 — success (always, even if no commits/files)
    1 — error (bad arguments, task not found, DB issue, etc.)
"""

import json
import os
import re
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
find_task_commits = _git_helpers.find_task_commits
extract_paths = _git_helpers.extract_paths
default_branch_of = _git_helpers.default_branch
commit_changed_files = _git_helpers.commit_changed_files
task_referenced_paths = _git_helpers.task_referenced_paths


def check_commits(task_id: int, repo_root: str) -> bool:
    """Return True if any commits reference [TASK-<id>] on any branch."""
    return bool(find_task_commits(task_id, repo_root, ["--all"]))


def check_default_branch_commits(task_id: int, repo_root: str) -> list:
    """Return commit SHAs on the default branch that reference [TASK-<id>]."""
    return find_task_commits(task_id, repo_root, [default_branch_of(repo_root)])


def _feature_branch_commits(task_id: int, repo_root: str, default_branch: str) -> list:
    """Return [TASK-<id>] commit SHAs reachable from any ref EXCEPT the default branch."""
    return find_task_commits(task_id, repo_root, ["--all", "--not", default_branch])


def find_existing_files(task_id: int, conn: sqlite3.Connection, repo_root: str) -> list:
    """Return paths referenced in task text / criteria specs that exist on disk."""
    found = []
    for p in task_referenced_paths(task_id, conn):
        abs_path = p if os.path.isabs(p) else os.path.join(repo_root, p)
        if os.path.exists(abs_path):
            found.append(p)
    return found


def all_active_criteria_complete(task_id: int, conn: sqlite3.Connection) -> bool:
    """True iff the task has at least one non-deferred criterion AND every non-deferred criterion is_completed=1.

    Deferred criteria (is_deferred=1) are excluded — they're intentionally skipped per the
    `tusk criteria skip` flow and don't count toward the salvage signal. A task with zero
    non-deferred criteria returns False (no salvage signal — vacuous truth is not informative).
    """
    row = conn.execute(
        "SELECT "
        "  COUNT(CASE WHEN COALESCE(is_deferred, 0) = 0 THEN 1 END) AS active, "
        "  COALESCE(SUM(CASE WHEN COALESCE(is_deferred, 0) = 0 AND is_completed = 1 THEN 1 ELSE 0 END), 0) AS done "
        "FROM acceptance_criteria WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return False
    active, done = row[0], row[1]
    return active > 0 and active == done


def main(argv: list) -> int:
    if len(argv) < 3:
        print("Usage: tusk check-deliverables <task_id>", file=sys.stderr)
        return 1

    db_path = argv[0]
    # argv[1] is config_path — reserved for future use
    task_id_raw = re.sub(r"^TASK-", "", argv[2], flags=re.IGNORECASE)
    try:
        task_id = int(task_id_raw)
    except ValueError:
        print(f"Invalid task ID: {argv[2]}", file=sys.stderr)
        return 1

    # repo_root is two levels up from the DB: tusk/tasks.db → tusk/ → repo_root
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))

    conn = get_connection(db_path)
    try:
        if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
            print(f"Task {task_id} not found", file=sys.stderr)
            return 1

        default_branch = default_branch_of(repo_root)
        default_commits = find_task_commits(task_id, repo_root, [default_branch])
        if default_commits:
            default_files = commit_changed_files(default_commits, repo_root)
            task_paths = set(task_referenced_paths(task_id, conn))
            feature_commits = _feature_branch_commits(task_id, repo_root, default_branch)
            feature_files = commit_changed_files(feature_commits, repo_root)
            scope = task_paths | feature_files
            # Downgrade only when we have a positive scope signal that fails to overlap.
            # Empty scope = no signal, not a downgrade trigger — preserve existing behavior.
            if scope and not (scope & default_files):
                recommendation = "merged_not_closed_low_confidence"
            else:
                recommendation = "merged_not_closed"
            output = {
                "commits_found": True,
                "files_found": False,
                "files": [],
                "default_branch_commits": default_commits,
                "default_branch_commit_files": sorted(default_files),
                "recommendation": recommendation,
            }
        elif check_commits(task_id, repo_root):
            output = {
                "commits_found": True,
                "files_found": False,
                "files": [],
                "default_branch_commits": [],
                "default_branch_commit_files": [],
                "recommendation": "commits_found",
            }
        else:
            files = find_existing_files(task_id, conn, repo_root)
            files_found = bool(files)
            if files_found:
                recommendation = "mark_done"
            elif all_active_criteria_complete(task_id, conn):
                recommendation = "criteria_complete_no_commits"
            else:
                recommendation = "implement_fresh"
            output = {
                "commits_found": False,
                "files_found": files_found,
                "files": files,
                "default_branch_commits": [],
                "default_branch_commit_files": [],
                "recommendation": recommendation,
            }

        print(dumps(output))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk check-deliverables <task_id>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
