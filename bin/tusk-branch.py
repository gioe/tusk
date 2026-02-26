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
    3. Check for an existing feature/TASK-<id>-* branch:
       - Multiple found → error listing all candidates
       - One found → warn and switch to it (skip creation)
       - None found → create feature/TASK-<id>-<slug>
    4. Print the branch name
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

    # Check for dirty working tree — only tracked modified/staged files need
    # stashing. Untracked files (status "??") carry over to the new branch
    # automatically and do not need to be stashed; including them in the dirty
    # check causes a spurious stash-pop failure when there is nothing to pop.
    status_result = run(["git", "status", "--porcelain"], check=False)
    dirty = any(
        line and not line.startswith("??")
        for line in status_result.stdout.splitlines()
    )
    if dirty:
        stash = run(
            ["git", "stash", "push", "-m", f"tusk-branch: auto-stash for TASK-{task_id}"],
            check=False,
        )
        if stash.returncode != 0:
            print(f"Error: git stash failed:\n{stash.stderr.strip()}", file=sys.stderr)
            return 2
        print(
            "Warning: uncommitted changes detected — stashed before creating branch.\n"
            "Run 'git stash pop' when ready to restore your changes.",
            file=sys.stderr,
        )

    # Checkout default branch and pull latest
    result = run(["git", "checkout", default_branch], check=False)
    if result.returncode != 0:
        print(f"Error: git checkout {default_branch} failed:\n{result.stderr.strip()}", file=sys.stderr)
        if dirty:
            pop = run(["git", "stash", "pop"], check=False)
            if pop.returncode == 0:
                print(
                    "Note: stash restored to working tree — you do not need to run git stash pop.",
                    file=sys.stderr,
                )
            else:
                print(
                    "Note: git stash pop failed — run 'git stash pop' manually to restore your changes.",
                    file=sys.stderr,
                )
        return 2

    result = run(["git", "pull", "origin", default_branch], check=False)
    if result.returncode != 0:
        print(f"Error: git pull origin {default_branch} failed:\n{result.stderr.strip()}", file=sys.stderr)
        if dirty:
            pop = run(["git", "stash", "pop"], check=False)
            if pop.returncode == 0:
                print(
                    "Note: stash restored to working tree — you do not need to run git stash pop.",
                    file=sys.stderr,
                )
            else:
                print(
                    "Note: git stash pop failed — run 'git stash pop' manually to restore your changes.",
                    file=sys.stderr,
                )
        return 2

    # Create feature branch — check if one already exists for this task
    branch_name = f"feature/TASK-{task_id}-{slug}"
    existing = run(["git", "branch", "--list", f"feature/TASK-{task_id}-*"], check=False)
    existing_branches: list[str] = []
    if existing.returncode == 0:
        for line in existing.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("* "):
                stripped = stripped[2:]
            if stripped:
                existing_branches.append(stripped)

    if len(existing_branches) > 1:
        names = ", ".join(existing_branches)
        print(
            f"Error: multiple existing branches found for TASK-{task_id}: {names}. "
            f"Delete all but one before running tusk branch.",
            file=sys.stderr,
        )
        if dirty:
            pop = run(["git", "stash", "pop"], check=False)
            if pop.returncode == 0:
                print(
                    "Note: stash restored to working tree — you do not need to run git stash pop.",
                    file=sys.stderr,
                )
            else:
                print(
                    "Note: git stash pop failed — run 'git stash pop' manually to restore your changes.",
                    file=sys.stderr,
                )
        return 2
    elif existing_branches:
        existing_branch = existing_branches[0]
        print(
            f"Warning: branch '{existing_branch}' already exists for TASK-{task_id}. "
            f"Switching to it instead of creating a new branch. "
            f"If you want a fresh branch, delete it first: git branch -D {existing_branch}",
            file=sys.stderr,
        )
        result = run(["git", "checkout", existing_branch], check=False)
        if result.returncode != 0:
            print(f"Error: git checkout {existing_branch} failed:\n{result.stderr.strip()}", file=sys.stderr)
            if dirty:
                pop = run(["git", "stash", "pop"], check=False)
                if pop.returncode == 0:
                    print(
                        "Note: stash restored to working tree — you do not need to run git stash pop.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "Note: git stash pop failed — run 'git stash pop' manually to restore your changes.",
                        file=sys.stderr,
                    )
            return 2
        branch_name = existing_branch
    else:
        result = run(["git", "checkout", "-b", branch_name], check=False)
        if result.returncode != 0:
            print(f"Error: git checkout -b {branch_name} failed:\n{result.stderr.strip()}", file=sys.stderr)
            if dirty:
                pop = run(["git", "stash", "pop"], check=False)
                if pop.returncode == 0:
                    print(
                        "Note: stash restored to working tree — you do not need to run git stash pop.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "Note: git stash pop failed — run 'git stash pop' manually to restore your changes.",
                        file=sys.stderr,
                    )
            return 2

    print(branch_name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
