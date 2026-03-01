#!/usr/bin/env python3
"""Finalize a task: close session, merge branch, push, clean up, and close task.

Called by the tusk wrapper:
    tusk merge <task_id> [--session <session_id>] [--pr] [--pr-number N]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — task_id [--session <session_id>] [--pr] [--pr-number N]

If --session is omitted, the open session for the task is auto-detected:
  - Exactly one open session → use it
  - No open sessions, but closed one exists → use most-recent closed session (warning)
  - No open sessions, no closed sessions → error with helpful message
  - Multiple open sessions → error listing all of them

Default behavior (merge.mode = local):
  1. Preflight: verify working tree is clean and feature branch exists (errors here leave session and task untouched)
  2. tusk session-close <session_id> (captures diff stats before branch change)
  3. git checkout <default_branch> && git pull
  4. git merge --ff-only feature/TASK-<id>-*
  5. git push
  6. git branch -d feature/TASK-<id>-*
  7. tusk task-done <id> --reason completed (--force if task-done warns)
  8. Print JSON with task details and unblocked_tasks array

--pr flag (or merge.mode = pr in config):
  Replaces steps 3-6 with: gh pr merge <pr_number> --squash --delete-branch
  Requires --pr-number.
"""

import json
import os
import sqlite3
import subprocess
import sys


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def detect_default_branch() -> str:
    """Detect the repo's default branch via remote HEAD, gh fallback, then 'main'."""
    run(["git", "remote", "set-head", "origin", "--auto"], check=False)
    result = run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().replace("refs/remotes/origin/", "")

    result = run(
        ["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    return "main"


def load_merge_mode(config_path: str) -> str:
    """Load merge.mode from config, defaulting to 'local'."""
    try:
        with open(config_path) as f:
            config = json.load(f)
        return config.get("merge", {}).get("mode", "local")
    except (FileNotFoundError, json.JSONDecodeError):
        return "local"


def find_task_branch(task_id: int) -> tuple[str | None, str | None]:
    """Return (branch_name, error_message). branch_name is None on error."""
    result = run(["git", "branch", "--list", f"feature/TASK-{task_id}-*"], check=False)
    if result.returncode != 0:
        return None, f"git branch --list failed: {result.stderr.strip()}"

    branches = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("* "):
            stripped = stripped[2:]
        if stripped:
            branches.append(stripped)

    if len(branches) == 0:
        return None, f"No branch found matching feature/TASK-{task_id}-*"
    if len(branches) > 1:
        names = ", ".join(branches)
        return None, (
            f"Multiple branches found for TASK-{task_id}: {names}. "
            "Delete all but one before running tusk merge."
        )
    return branches[0], None


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(
            "Usage: tusk merge <task_id> [--session <session_id>] [--pr] [--pr-number N]",
            file=sys.stderr,
        )
        return 1

    # DB path is used for read-only session lookup (auto-detect); write ops
    # (session-close, task-done) are delegated to tusk subprocesses.
    _db_path = argv[0]
    config_path = argv[1]

    try:
        task_id = int(argv[2])
    except ValueError:
        print(f"Error: Invalid task ID: {argv[2]}", file=sys.stderr)
        return 1

    # Parse remaining flags
    remaining = argv[3:]
    session_id = None
    use_pr = False
    pr_number = None

    i = 0
    while i < len(remaining):
        if remaining[i] == "--session":
            if i + 1 >= len(remaining):
                print("Error: --session requires a value", file=sys.stderr)
                return 1
            try:
                session_id = int(remaining[i + 1])
            except ValueError:
                print(f"Error: Invalid session ID: {remaining[i + 1]}", file=sys.stderr)
                return 1
            i += 2
        elif remaining[i] == "--pr":
            use_pr = True
            i += 1
        elif remaining[i] == "--pr-number":
            if i + 1 >= len(remaining):
                print("Error: --pr-number requires a value", file=sys.stderr)
                return 1
            try:
                pr_number = int(remaining[i + 1])
            except ValueError:
                print(f"Error: Invalid PR number: {remaining[i + 1]}", file=sys.stderr)
                return 1
            i += 2
        else:
            print(f"Error: Unknown argument: {remaining[i]}", file=sys.stderr)
            return 1

    if session_id is None:
        # Auto-detect the open session for this task
        try:
            with sqlite3.connect(_db_path) as conn:
                rows = conn.execute(
                    "SELECT id, started_at FROM task_sessions WHERE task_id = ? AND ended_at IS NULL ORDER BY id",
                    (task_id,),
                ).fetchall()
                if len(rows) == 0:
                    closed_rows = conn.execute(
                        "SELECT id FROM task_sessions WHERE task_id = ? AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                        (task_id,),
                    ).fetchall()
                else:
                    closed_rows = []
        except sqlite3.Error as e:
            print(f"Error: Could not query sessions: {e}", file=sys.stderr)
            return 1

        if len(rows) == 0:
            if len(closed_rows) == 0:
                print(
                    f"Error: No open session found for task {task_id}. "
                    "Start a session with `tusk task-start` or pass --session <id> explicitly.",
                    file=sys.stderr,
                )
                return 1
            session_id = closed_rows[0][0]
            print(
                f"Warning: No open session found for task {task_id}; "
                f"falling back to last closed session {session_id}.",
                file=sys.stderr,
            )
        elif len(rows) > 1:
            lines = "\n".join(f"  session {r[0]}  (started {r[1]})" for r in rows)
            print(
                f"Error: Multiple open sessions found for task {task_id}:\n{lines}\n"
                "Close all but one, or pass --session <id> explicitly.",
                file=sys.stderr,
            )
            return 1
        else:
            session_id = rows[0][0]
            print(f"Auto-detected session {session_id} for task {task_id}.", file=sys.stderr)

    # Resolve merge mode (config can force PR mode)
    merge_mode = load_merge_mode(config_path)
    if merge_mode == "pr":
        use_pr = True

    if use_pr and pr_number is None:
        print("Error: --pr-number <N> is required when using PR mode", file=sys.stderr)
        return 1

    # Locate the tusk binary (sibling of this script in the same bin/ directory)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    tusk_bin = os.path.join(script_dir, "tusk")

    # Preflight checks — abort before touching session or task state
    # Step 1a: Detect feature branch
    branch_name, err = find_task_branch(task_id)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    # Step 1b (local mode only): Abort if working tree is dirty
    # Only check for staged/unstaged changes to tracked files; untracked files are not
    # uncommitted changes and should not block a merge.
    if not use_pr:
        unstaged = run(["git", "diff", "--quiet"], check=False)
        staged = run(["git", "diff", "--cached", "--quiet"], check=False)
        if unstaged.returncode not in (0, 1) or staged.returncode not in (0, 1):
            # returncode > 1 means git itself failed
            err = unstaged.stderr.strip() or staged.stderr.strip()
            print(f"Error: git diff failed:\n{err}", file=sys.stderr)
            return 1
        if unstaged.returncode != 0 or staged.returncode != 0:
            print(
                "Error: Working tree has uncommitted changes — cannot proceed with merge.\n"
                "Please stash or commit your changes first:\n"
                "  git stash        # stash and restore later\n"
                "  git stash pop    # restore after merge\n"
                "  git add . && git commit -m 'wip'   # commit as work-in-progress",
                file=sys.stderr,
            )
            return 1

    print(f"Found branch: {branch_name}", file=sys.stderr)

    # Step 2: Close the session (captures git diff stats while on feature branch)
    print(f"Closing session {session_id}...", file=sys.stderr)
    result = run([tusk_bin, "session-close", str(session_id)], check=False)
    session_was_closed = result.returncode == 0
    if result.returncode != 0:
        if "already closed" in result.stderr:
            print(f"Warning: session {session_id} is already closed — continuing.", file=sys.stderr)
        else:
            print(f"Error: session-close failed:\n{result.stderr.strip()}", file=sys.stderr)
            return 2

    if use_pr:
        # PR mode: delegate to gh pr merge
        print(f"Merging PR #{pr_number} via gh...", file=sys.stderr)
        result = run(
            ["gh", "pr", "merge", str(pr_number), "--squash", "--delete-branch"],
            check=False,
        )
        if result.returncode != 0:
            print(f"Error: gh pr merge failed:\n{result.stderr.strip()}", file=sys.stderr)
            return 2
        if result.stdout.strip():
            print(result.stdout.strip(), file=sys.stderr)
    else:
        # Local mode: ff-only merge
        default_branch = detect_default_branch()
        print(f"Merging {branch_name} into {default_branch} (ff-only)...", file=sys.stderr)

        # Step 3: Checkout default branch
        result = run(["git", "checkout", default_branch], check=False)
        if result.returncode != 0:
            print(
                f"Error: git checkout {default_branch} failed:\n{result.stderr.strip()}",
                file=sys.stderr,
            )
            return 2

        # Step 4: Pull latest
        result = run(["git", "pull", "origin", default_branch], check=False)
        if result.returncode != 0:
            print(f"Error: git pull failed:\n{result.stderr.strip()}", file=sys.stderr)
            # Restore feature branch so user can investigate
            run(["git", "checkout", branch_name], check=False)
            return 2

        # Step 4 (cont): Fast-forward merge
        result = run(["git", "merge", "--ff-only", branch_name], check=False)
        if result.returncode != 0:
            print(
                f"Error: git merge --ff-only {branch_name} failed:\n{result.stderr.strip()}\n"
                "The feature branch cannot be fast-forward merged. "
                "Rebase the branch onto the default branch first, "
                "or use --pr mode for a squash merge.",
                file=sys.stderr,
            )
            # Restore feature branch so user can investigate
            run(["git", "checkout", branch_name], check=False)
            return 2

        # Step 5: Push
        result = run(["git", "push", "origin", default_branch], check=False)
        if result.returncode != 0:
            print(
                f"Error: git push failed:\n{result.stderr.strip()}\n"
                f"The branch has been merged locally but not pushed.\n"
                f"  Retry: git push origin {default_branch}\n"
                f"  Undo:  git reset --hard HEAD~1 && git checkout {branch_name}",
                file=sys.stderr,
            )
            return 2

        # Step 6: Delete feature branch
        result = run(["git", "branch", "-d", branch_name], check=False)
        if result.returncode != 0:
            # Non-fatal: branch is already merged, warn and continue
            print(
                f"Warning: git branch -d {branch_name} failed:\n{result.stderr.strip()}",
                file=sys.stderr,
            )

    # Step 7: Close the task — run without --force first to surface any warnings
    print(f"Closing task {task_id}...", file=sys.stderr)
    result = run(
        [tusk_bin, "task-done", str(task_id), "--reason", "completed"],
        check=False,
    )
    if result.returncode == 3:
        # task-done has warnings (uncompleted criteria or missing commit hashes);
        # print them so the user is aware, then close with --force
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        result = run(
            [tusk_bin, "task-done", str(task_id), "--reason", "completed", "--force"],
            check=False,
        )
    if result.returncode != 0:
        print(f"Error: task-done failed:\n{result.stderr.strip()}", file=sys.stderr)
        return 2

    # Step 8: Forward the task-done JSON to stdout
    try:
        task_done_result = json.loads(result.stdout)
    except json.JSONDecodeError:
        # task-done produced non-JSON output; print as-is
        if result.stdout.strip():
            print(result.stdout.strip())
        return 0

    # tusk session-close already closed the session before task-done ran, so
    # task-done always sees 0 open sessions. Correct the counter here.
    # When session-close returned non-zero with "already closed", session_was_closed
    # is False and sessions_closed stays 0 — accurate, since this invocation did not
    # close anything. merge always operates on exactly one session_id.
    if session_was_closed:
        task_done_result["sessions_closed"] = 1  # merge always operates on one session

    print(json.dumps(task_done_result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
