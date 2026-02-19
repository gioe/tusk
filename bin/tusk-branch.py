#!/usr/bin/env python3
"""Create a feature branch for a task.

Called by the tusk wrapper:
    tusk branch <task_id> <slug>

Arguments received from tusk:
    sys.argv[1] — repo root (unused, kept for dispatch consistency)
    sys.argv[2:] — task_id and slug

Steps:
    1. Detect the repo's default branch (remote HEAD → gh fallback → "main")
    2. Check out the default branch and pull latest
    3. Create feature/TASK-<id>-<slug>
    4. Print the created branch name
"""

import subprocess
import sys


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def detect_default_branch() -> str:
    """Detect the repo's default branch via remote HEAD, gh fallback, then 'main'."""
    # Try remote HEAD
    run(["git", "remote", "set-head", "origin", "--auto"], check=False)
    result = run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], check=False)
    if result.returncode == 0 and result.stdout.strip():
        # refs/remotes/origin/main → main
        return result.stdout.strip().replace("refs/remotes/origin/", "")

    # Try gh CLI fallback
    result = run(
        ["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    return "main"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("Usage: tusk branch <task_id> <slug>", file=sys.stderr)
        return 1

    # argv[0] is repo_root (unused)
    task_id_str = argv[1]
    slug = argv[2]

    try:
        task_id = int(task_id_str)
    except ValueError:
        print(f"Error: Invalid task ID: {task_id_str}", file=sys.stderr)
        return 1

    if not slug.strip():
        print("Error: Slug must not be empty", file=sys.stderr)
        return 1

    # Detect default branch
    default_branch = detect_default_branch()

    # Checkout default branch and pull latest
    result = run(["git", "checkout", default_branch], check=False)
    if result.returncode != 0:
        print(f"Error: git checkout {default_branch} failed:\n{result.stderr.strip()}", file=sys.stderr)
        return 2

    result = run(["git", "pull", "origin", default_branch], check=False)
    if result.returncode != 0:
        print(f"Error: git pull origin {default_branch} failed:\n{result.stderr.strip()}", file=sys.stderr)
        return 2

    # Create feature branch
    branch_name = f"feature/TASK-{task_id}-{slug}"
    result = run(["git", "checkout", "-b", branch_name], check=False)
    if result.returncode != 0:
        print(f"Error: git checkout -b {branch_name} failed:\n{result.stderr.strip()}", file=sys.stderr)
        return 2

    print(branch_name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
