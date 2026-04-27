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
        "recommendation": "commits_found" | "merged_not_closed" | "merged_not_closed_low_confidence" | "mark_done" | "implement_fresh"
    }

Recommendations:
    "commits_found"                       — commits referencing this task exist on a non-default branch — normal path
    "merged_not_closed"                   — commits already on the default branch and their diff overlaps with task scope (or there is no scope signal to compare) — skip implementation, go straight to finalize
    "merged_not_closed_low_confidence"    — commits exist on the default branch but their diff doesn't overlap with files referenced in the task or with files modified on any feature branch — likely a [TASK-N] prefix-match false positive — verify before acting
    "mark_done"                           — no commits, but deliverable files found on disk — mark criteria done and merge
    "implement_fresh"                     — no commits, no files found — proceed with implementation

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

# Regex to extract candidate file paths from unstructured text.
# Matches tokens that start with a path-like prefix and contain at least one dot
# (suggesting a filename with an extension).
_PATH_RE = re.compile(
    r'(?:^|[\s\'"`(,])('
    r'(?:\./|\.\./|\.claude/|\.claude\\|bin/|skills[-_]?internal/|skills/|tests?/|docs?/|src/'
    r'|(?!\w+://)\w[\w._-]*/'  # any directory prefix that is not a URL protocol
    r')'
    r'[\w./_-]+'
    r')',
    re.MULTILINE,
)


def _extract_paths(text: str) -> list:
    """Extract candidate file paths from free-form text."""
    if not text:
        return []
    paths = []
    for m in _PATH_RE.finditer(text):
        p = m.group(1).strip().rstrip('.,;:\'"`)')
        # Require an extension so we don't chase bare directory names
        if p and '.' in os.path.basename(p) and '://' not in p:
            paths.append(p)
    return paths


def _default_branch(repo_root: str) -> str:
    """Detect the default branch: symbolic-ref → gh fallback → 'main'.

    Mirrors cmd_git_default_branch in bin/tusk.
    """
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo_root,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().removeprefix("refs/remotes/origin/")
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo_root,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "main"


def check_commits(task_id: int, repo_root: str) -> bool:
    """Return True if any commits reference [TASK-<id>] on any branch."""
    return bool(find_task_commits(task_id, repo_root, ["--all"]))


def check_default_branch_commits(task_id: int, repo_root: str) -> list:
    """Return commit SHAs on the default branch that reference [TASK-<id>]."""
    return find_task_commits(task_id, repo_root, [_default_branch(repo_root)])


def _feature_branch_commits(task_id: int, repo_root: str, default_branch: str) -> list:
    """Return [TASK-<id>] commit SHAs reachable from any ref EXCEPT the default branch."""
    return find_task_commits(task_id, repo_root, ["--all", "--not", default_branch])


def _commit_changed_files(commits: list, repo_root: str) -> set:
    """Return the union of changed file paths across the given commits."""
    files: set = set()
    for sha in commits:
        result = subprocess.run(
            ["git", "show", "--name-only", "--format=", sha],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=repo_root,
        )
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            line = line.strip()
            if line:
                files.add(line)
    return files


def _task_referenced_paths(task_id: int, conn: sqlite3.Connection) -> list:
    """Return paths referenced in task summary/description/criteria/specs (no existence check)."""
    row = conn.execute(
        "SELECT summary, description FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not row:
        return []

    criteria_rows = conn.execute(
        "SELECT criterion, verification_spec FROM acceptance_criteria WHERE task_id = ?",
        (task_id,),
    ).fetchall()

    texts = [row["summary"] or "", row["description"] or ""]
    for cr in criteria_rows:
        texts.append(cr["criterion"] or "")
        texts.append(cr["verification_spec"] or "")

    candidates = []
    seen: set = set()
    for text in texts:
        for p in _extract_paths(text):
            if p not in seen:
                seen.add(p)
                candidates.append(p)
    return candidates


def find_existing_files(task_id: int, conn: sqlite3.Connection, repo_root: str) -> list:
    """Return paths referenced in task text / criteria specs that exist on disk."""
    found = []
    for p in _task_referenced_paths(task_id, conn):
        abs_path = p if os.path.isabs(p) else os.path.join(repo_root, p)
        if os.path.exists(abs_path):
            found.append(p)
    return found


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

        default_branch = _default_branch(repo_root)
        default_commits = find_task_commits(task_id, repo_root, [default_branch])
        if default_commits:
            default_files = _commit_changed_files(default_commits, repo_root)
            task_paths = set(_task_referenced_paths(task_id, conn))
            feature_commits = _feature_branch_commits(task_id, repo_root, default_branch)
            feature_files = _commit_changed_files(feature_commits, repo_root)
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
            output = {
                "commits_found": False,
                "files_found": files_found,
                "files": files,
                "default_branch_commits": [],
                "default_branch_commit_files": [],
                "recommendation": "mark_done" if files_found else "implement_fresh",
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
