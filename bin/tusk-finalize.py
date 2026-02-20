#!/usr/bin/env python3
"""Consolidate post-merge finalization into a single CLI command.

Called by the tusk wrapper:
    tusk finalize <task_id> --session <session_id> --pr-url <url> --pr-number <number>

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — task_id and flags

Performs all finalization steps for a completed task:
  1. Set github_pr on the task
  2. Close the session (delegates to tusk session-close for diff stats)
  3. Merge the PR via gh pr merge --squash --delete-branch
  4. Mark task Done via tusk task-done --reason completed
  5. Print JSON result with task details and newly unblocked tasks
"""

import json
import subprocess
import sys


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(
            "Usage: tusk finalize <task_id> --session <id> --pr-url <url> --pr-number <number>",
            file=sys.stderr,
        )
        return 1

    db_path = argv[0]
    config_path = argv[1]

    try:
        task_id = int(argv[2])
    except ValueError:
        print(f"Error: Invalid task ID: {argv[2]}", file=sys.stderr)
        return 1

    # Parse flags
    remaining = argv[3:]
    session_id = None
    pr_url = None
    pr_number = None
    i = 0
    while i < len(remaining):
        if remaining[i] == "--session" and i + 1 < len(remaining):
            session_id = remaining[i + 1]
            i += 2
        elif remaining[i] == "--pr-url" and i + 1 < len(remaining):
            pr_url = remaining[i + 1]
            i += 2
        elif remaining[i] == "--pr-number" and i + 1 < len(remaining):
            pr_number = remaining[i + 1]
            i += 2
        else:
            print(f"Error: Unknown argument: {remaining[i]}", file=sys.stderr)
            return 1

    if not session_id:
        print("Error: --session is required", file=sys.stderr)
        return 1
    if not pr_url:
        print("Error: --pr-url is required", file=sys.stderr)
        return 1
    if not pr_number:
        print("Error: --pr-number is required", file=sys.stderr)
        return 1

    # ── Step 1: Set github_pr on the task ─────────────────────────────
    result = run(
        ["tusk", "task-update", str(task_id), "--github-pr", pr_url],
        check=False,
    )
    if result.returncode != 0:
        print(f"Error: Failed to set github_pr:\n{result.stderr.strip()}", file=sys.stderr)
        return 2

    # ── Step 2: Close the session ─────────────────────────────────────
    result = run(
        ["tusk", "session-close", session_id],
        check=False,
    )
    if result.returncode != 0:
        # Session may already be closed — warn but continue
        msg = result.stderr.strip() or result.stdout.strip()
        print(f"Warning: session-close returned non-zero: {msg}", file=sys.stderr)

    # ── Step 3: Merge the PR ──────────────────────────────────────────
    result = run(
        ["gh", "pr", "merge", pr_number, "--squash", "--delete-branch"],
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        print(f"Error: gh pr merge failed:\n{stderr}", file=sys.stderr)
        return 3

    # ── Step 4: Mark task Done ────────────────────────────────────────
    result = run(
        ["tusk", "task-done", str(task_id), "--reason", "completed", "--force"],
        check=False,
    )
    if result.returncode != 0:
        print(f"Error: task-done failed:\n{result.stderr.strip()}", file=sys.stderr)
        return 4

    # task-done returns JSON with task details and unblocked tasks
    try:
        task_done_result = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        task_done_result = {"raw_output": result.stdout.strip()}

    # ── Step 5: Print result ──────────────────────────────────────────
    output = {
        "task_id": task_id,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "session_closed": session_id,
        "merged": True,
        "task_done": task_done_result,
    }
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
