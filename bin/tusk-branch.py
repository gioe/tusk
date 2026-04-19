#!/usr/bin/env python3
"""Create a feature branch for a task.

Called by the tusk wrapper:
    tusk branch <task_id> <slug> [--pop-stash]

Arguments received from tusk:
    sys.argv[1] — repo root, used to locate the tusk DB for WAL checkpoint
    sys.argv[2:] — task_id, slug, and optional flags

Steps:
    1. Detect the repo's default branch (remote HEAD → gh fallback → "main")
    2. Check out the default branch and pull latest
    3. Check for an existing feature/TASK-<id>-* branch:
       - Multiple found → error listing all candidates
       - One found → warn and switch to it (skip creation)
       - None found → create feature/TASK-<id>-<slug>
    4. Print the branch name

Stash behavior:
    When the working tree is dirty, tusk branch auto-stashes the changes before
    switching branches. By default the stash is left intact (safer when the
    orphan changes belong to a previous task) and the stash ref/message is
    printed so the user can pop manually. Pass --pop-stash to restore the
    previous behavior of popping the stash onto the new branch at the end.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py and tusk-git-helpers.py

_db_lib = tusk_loader.load("tusk-db-lib")
checkpoint_wal = _db_lib.checkpoint_wal

_git_helpers = tusk_loader.load("tusk-git-helpers")
_is_remote_unreachable = _git_helpers._is_remote_unreachable
_UNREACHABLE_REMOTE_PATTERNS = _git_helpers._UNREACHABLE_REMOTE_PATTERNS
_UNREACHABLE_REMOTE_REGEX = _git_helpers._UNREACHABLE_REMOTE_REGEX


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def _has_remote(name: str = "origin") -> bool:
    """Return True if the named git remote exists."""
    result = run(["git", "remote", "get-url", name], check=False)
    return result.returncode == 0


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


def _try_pop_stash(current_branch: str | None = None) -> None:
    """Attempt to pop the auto-stash and notify the user of the outcome.

    Only invoked when --pop-stash is passed; the default path leaves the stash
    intact. If *current_branch* is provided, append a note reminding the user
    which branch they are currently on (useful when checkout succeeded but a
    later step failed, leaving them on the default branch rather than their
    original branch).
    """
    branch_note = (
        f" You are now on '{current_branch}'; switch back to your original branch before continuing."
        if current_branch
        else ""
    )
    pop = run(["git", "stash", "pop"], check=False)
    if pop.returncode == 0:
        status = run(["git", "status", "--porcelain"], check=False)
        restored_files = []
        if status.returncode == 0:
            for line in status.stdout.splitlines():
                if line.strip():
                    restored_files.append(line[3:].strip())
        if restored_files:
            file_list = "\n  ".join(restored_files)
            print(
                f"Warning: stash restored to working tree — these changes may belong to a different task.{branch_note}\n  {file_list}",
                file=sys.stderr,
            )
        else:
            print(
                f"Note: stash restored to working tree — you do not need to run git stash pop.{branch_note}",
                file=sys.stderr,
            )
    else:
        print(
            f"Note: git stash pop failed — run 'git stash pop' manually to restore your changes.{branch_note}",
            file=sys.stderr,
        )


def _emit_stash_preserved(stash_msg: str, current_branch: str | None = None) -> None:
    """Print a note that the auto-stash was left intact (default path)."""
    branch_note = f" You are now on '{current_branch}'." if current_branch else ""
    print(
        f"Note: orphan changes saved as stash@{{0}}: {stash_msg}.{branch_note}\n"
        f"  If they belong to this task, restore with: git stash pop stash@{{0}}\n"
        f"  Otherwise drop with: git stash drop stash@{{0}} (or re-run with --pop-stash to restore automatically).",
        file=sys.stderr,
    )


def _handle_stash_exit(
    pop_stash: bool, stash_msg: str, current_branch: str | None = None
) -> None:
    """Dispatch stash cleanup based on the --pop-stash flag."""
    if pop_stash:
        _try_pop_stash(current_branch=current_branch)
    else:
        _emit_stash_preserved(stash_msg, current_branch=current_branch)


def main(argv: list[str]) -> int:
    pop_stash = False
    positional: list[str] = []
    for a in argv:
        if a == "--pop-stash":
            pop_stash = True
        else:
            positional.append(a)

    if len(positional) < 3:
        print("Usage: tusk branch <task_id> <slug> [--pop-stash]", file=sys.stderr)
        return 1

    # positional[0] is repo_root — used to locate the tusk DB for WAL checkpoint
    repo_root = positional[0]
    task_id_str = positional[1]
    slug = positional[2]

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
    # .claude/ files (e.g. generated .pyc bytecode) are included: if they are
    # tracked and modified, git pull (or git pull --rebase when pull.rebase is
    # set) will refuse to run. Stashing them is safe here because this script
    # runs entirely in-process — the stash is
    # held only for the duration of the git operations below and is popped onto
    # the new branch before the script exits.
    status_result = run(["git", "status", "--porcelain"], check=False)
    dirty = any(
        line and not line.startswith("??")
        for line in status_result.stdout.splitlines()
    )
    stash_msg = f"tusk-branch: auto-stash for TASK-{task_id}"
    if dirty:
        # Checkpoint the WAL before stashing so that any in-flight SQLite
        # writes are flushed to the main DB file. Without this, a git stash
        # that reverts tasks.db to a pre-WAL snapshot silently abandons rows
        # written since the last automatic checkpoint.
        db_path = os.path.join(repo_root, "tusk", "tasks.db")
        checkpoint_wal(db_path)
        stash = run(
            ["git", "stash", "push", "-m", stash_msg],
            check=False,
        )
        if stash.returncode != 0:
            print(f"Error: git stash failed:\n{stash.stderr.strip()}", file=sys.stderr)
            return 2
        print(
            "Warning: uncommitted changes detected — stashed before switching branches.",
            file=sys.stderr,
        )

    # Checkout default branch and pull latest
    result = run(["git", "checkout", default_branch], check=False)
    if result.returncode != 0:
        print(f"Error: git checkout {default_branch} failed:\n{result.stderr.strip()}", file=sys.stderr)
        if dirty:
            _handle_stash_exit(pop_stash, stash_msg)
        return 2

    if _has_remote():
        result = run(["git", "pull", "origin", default_branch], check=False)
        if result.returncode != 0:
            if _is_remote_unreachable(result.stderr):
                print(
                    f"Warning: could not reach origin — skipping pull. "
                    f"Branching from local '{default_branch}'.\n  {result.stderr.strip()}",
                    file=sys.stderr,
                )
            else:
                print(f"Error: git pull origin {default_branch} failed:\n{result.stderr.strip()}", file=sys.stderr)
                if dirty:
                    _handle_stash_exit(pop_stash, stash_msg, current_branch=default_branch)
                return 2
    else:
        print(
            "Warning: no git remote 'origin' configured — skipping pull. "
            "Branching from local HEAD.",
            file=sys.stderr,
        )

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
            _handle_stash_exit(pop_stash, stash_msg, current_branch=default_branch)
        return 2
    elif existing_branches:
        existing_branch = existing_branches[0]

        # Check whether the existing branch tip is already an ancestor of the
        # default branch (i.e. it was previously merged).  If so, switching to
        # it would land the user on a stale branch whose content is identical
        # to the default branch, causing confusing stash-pop conflicts.
        tip = run(["git", "rev-parse", existing_branch], check=False)
        is_merged = (
            tip.returncode == 0
            and run(
                ["git", "merge-base", "--is-ancestor", tip.stdout.strip(), default_branch],
                check=False,
            ).returncode == 0
        )

        if is_merged:
            print(
                f"Warning: branch '{existing_branch}' for TASK-{task_id} appears to be already "
                f"merged into '{default_branch}'.",
                file=sys.stderr,
            )
            if sys.stdin.isatty():
                print(
                    f"Delete it and create a fresh '{branch_name}'? [y/N] ",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
                answer = sys.stdin.readline().strip().lower()
            else:
                answer = "n"

            if answer != "y":
                print(
                    f"Aborting — stale branch '{existing_branch}' left intact. "
                    f"Delete it manually with: git branch -D {existing_branch}",
                    file=sys.stderr,
                )
                if dirty:
                    _handle_stash_exit(pop_stash, stash_msg, current_branch=default_branch)
                return 2

            delete_result = run(["git", "branch", "-D", existing_branch], check=False)
            if delete_result.returncode != 0:
                print(
                    f"Error: could not delete '{existing_branch}':\n{delete_result.stderr.strip()}",
                    file=sys.stderr,
                )
                if dirty:
                    _handle_stash_exit(pop_stash, stash_msg, current_branch=default_branch)
                return 2

            result = run(["git", "checkout", "-b", branch_name], check=False)
            if result.returncode != 0:
                print(
                    f"Error: git checkout -b {branch_name} failed:\n{result.stderr.strip()}",
                    file=sys.stderr,
                )
                if dirty:
                    _handle_stash_exit(pop_stash, stash_msg, current_branch=default_branch)
                return 2
        else:
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
                    _handle_stash_exit(pop_stash, stash_msg, current_branch=default_branch)
                return 2
            branch_name = existing_branch
    else:
        result = run(["git", "checkout", "-b", branch_name], check=False)
        if result.returncode != 0:
            print(f"Error: git checkout -b {branch_name} failed:\n{result.stderr.strip()}", file=sys.stderr)
            if dirty:
                _handle_stash_exit(pop_stash, stash_msg, current_branch=default_branch)
            return 2

    # Default: leave the stash intact so orphan changes that belong to a
    # previous task don't silently ride along to the new branch. With
    # --pop-stash, restore the previous behavior of popping onto the new
    # branch (useful when the dirty changes really do belong to this task).
    if dirty:
        _handle_stash_exit(pop_stash, stash_msg)

    print(branch_name)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not os.path.isdir(sys.argv[1]):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk branch <task_id> <slug>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
