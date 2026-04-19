#!/usr/bin/env python3
"""Compute the git diff range for /review-commits against a task's branch.

Given a task_id, determine the most meaningful git diff range for the review:

    1. Primary range — ``<default_branch>...HEAD``, resolved by shelling out
       to ``tusk git-default-branch`` so the remote-HEAD → gh → "main"
       detection stays in lockstep with the wrapper.
    2. Fallback — if the primary range has an empty diff (e.g. the feature
       branch has already been merged into the default branch and deleted),
       scan ``git log`` for the 50 most recent commits whose message contains
       ``[TASK-<id>]`` and build a range ``<oldest>^..<newest>`` from that
       set. This mirrors Step 3 of ``/review-commits``.

If both paths yield an empty diff — no ``[TASK-<id>]`` commits found in
recent history, or the recovered range is still empty — exit non-zero with
an error message on stderr. The review cannot proceed without a diff.

Usage:
    tusk review-diff-range <task_id>

Arguments received from tusk:
    sys.argv[1] — DB path (used only to resolve repo_root)
    sys.argv[2] — config path (unused)
    sys.argv[3] — task_id (integer or TASK-NNN prefix form)

Output JSON (stdout on success):
    {
        "range": "<default>...HEAD" | "<sha>^..<sha>",
        "diff_lines": <int>,
        "summary": "<first 120 chars of git diff output>",
        "recovered_from_task_commits": <bool>
    }

Exit codes:
    0 — success (JSON on stdout)
    1 — bad arguments, or no diff recoverable (error on stderr)
"""

import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps

SUMMARY_CHARS = 120
TASK_COMMIT_LIMIT = 50

_TUSK_WRAPPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk")


def _git(args: list, repo_root: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo_root,
    )


def default_branch(repo_root: str) -> str:
    """Resolve the default branch by calling ``tusk git-default-branch``.

    Shelling out to the wrapper (rather than re-implementing the symbolic-ref
    → gh → "main" cascade here) keeps this helper in lockstep with every
    other caller of ``tusk git-default-branch`` across the codebase.
    """
    r = subprocess.run(
        [_TUSK_WRAPPER, "git-default-branch"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo_root,
    )
    branch = (r.stdout or "").strip()
    return branch or "main"


def compute_range(task_id: int, repo_root: str) -> dict:
    """Return the diff-range payload for this task, or raise on empty diff."""
    base = default_branch(repo_root)
    primary = f"{base}...HEAD"

    primary_result = _git(["diff", primary], repo_root)
    diff_out = primary_result.stdout if primary_result.returncode == 0 else ""
    diff_lines = diff_out.count("\n") if diff_out else 0
    if diff_lines > 0:
        return {
            "range": primary,
            "diff_lines": diff_lines,
            "summary": diff_out[:SUMMARY_CHARS],
            "recovered_from_task_commits": False,
        }

    # Primary range is empty — recover from [TASK-N] commits in recent history.
    log_result = _git(
        [
            "log",
            "--format=%H",
            f"--grep=\\[TASK-{task_id}\\]",
            "-n",
            str(TASK_COMMIT_LIMIT),
        ],
        repo_root,
    )
    commits = [c for c in (log_result.stdout or "").splitlines() if c.strip()]
    if not commits:
        raise SystemExit(
            f"No changes found — [TASK-{task_id}] commits not detected in recent "
            "git log. The diff range cannot be determined automatically. Confirm "
            "the correct commit range manually and re-run."
        )

    newest = commits[0]
    oldest = commits[-1]
    fallback = f"{oldest}^..{newest}"

    fallback_result = _git(["diff", fallback], repo_root)
    diff_out = fallback_result.stdout or ""
    diff_lines = diff_out.count("\n")
    if diff_lines == 0:
        raise SystemExit("No changes found compared to the base branch.")

    return {
        "range": fallback,
        "diff_lines": diff_lines,
        "summary": diff_out[:SUMMARY_CHARS],
        "recovered_from_task_commits": True,
    }


def main(argv: list) -> int:
    if len(argv) < 3:
        print("Usage: tusk review-diff-range <task_id>", file=sys.stderr)
        return 1

    db_path = argv[0]
    # argv[1] is config_path — reserved for future use

    parser = argparse.ArgumentParser(
        prog="tusk review-diff-range",
        description="Compute the git diff range for /review-commits against a task's branch",
    )
    parser.add_argument("task_id", help="Task ID (integer or TASK-NNN prefix form)")
    args = parser.parse_args(argv[2:])

    task_id_raw = re.sub(r"^TASK-", "", args.task_id, flags=re.IGNORECASE)
    try:
        task_id = int(task_id_raw)
    except ValueError:
        print(f"Invalid task ID: {args.task_id}", file=sys.stderr)
        return 1

    # repo_root is two levels up from the DB: tusk/tasks.db → tusk/ → repo_root
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))

    try:
        result = compute_range(task_id, repo_root)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
        return 1

    print(dumps(result))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk review-diff-range <task_id>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
