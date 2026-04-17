#!/usr/bin/env python3
"""Verify that the agent-sandbox permissions required by /review-commits are
declared in `.claude/settings.json`.

Read order:
    1. `<repo_root>/.claude/settings.json` on disk.
    2. If missing, fall back to `git show HEAD:.claude/settings.json`
       (handles `tusk branch` stashing uncommitted changes).

Outputs (single line to stdout) + exit code:
    - MISSING: .claude/settings.json not found on disk or in HEAD — no permissions.allow configured
        → exit 1
    - MISSING: .claude/settings.json on disk is not valid JSON
        → exit 1
    - MISSING: .claude/settings.json in HEAD is not valid JSON
        → exit 1
    - MISSING: permissions.allow is not a list — no permissions.allow configured
        → exit 1
    - MISSING: <entry>, <entry>, ...
        → exit 1
    - OK
        → exit 0

Usage:
    tusk review-check-perms
"""

import json
import os
import subprocess
import sys

REQUIRED_PERMISSIONS = [
    "Bash(git diff:*)",
    "Bash(git remote:*)",
    "Bash(git symbolic-ref:*)",
    "Bash(git branch:*)",
    "Bash(tusk review:*)",
]


def _load_settings(repo_root: str) -> dict | None:
    """Load .claude/settings.json from disk; fall back to git show HEAD:.

    Returns the parsed dict, or None if the file is missing from both disk and
    HEAD or cannot be parsed. Writes the appropriate MISSING: line to stdout
    before returning None.
    """
    path = os.path.join(repo_root, ".claude", "settings.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        print("MISSING: .claude/settings.json on disk is not valid JSON")
        return None

    r = subprocess.run(
        ["git", "show", "HEAD:.claude/settings.json"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if r.returncode != 0:
        print(
            "MISSING: .claude/settings.json not found on disk or in HEAD — "
            "no permissions.allow configured"
        )
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        print("MISSING: .claude/settings.json in HEAD is not valid JSON")
        return None


def check(repo_root: str) -> int:
    settings = _load_settings(repo_root)
    if settings is None:
        return 1

    perms = settings.get("permissions", {})
    if not isinstance(perms, dict):
        print("MISSING: permissions is not an object — no permissions.allow configured")
        return 1
    allow = perms.get("allow", [])
    if not isinstance(allow, list):
        print("MISSING: permissions.allow is not a list — no permissions.allow configured")
        return 1
    missing = [p for p in REQUIRED_PERMISSIONS if p not in allow]
    if missing:
        print("MISSING: " + ", ".join(missing))
        return 1

    print("OK")
    return 0


def main(argv: list) -> int:
    # argv[0] = db_path, argv[1] = config_path (both unused — this script only
    # inspects .claude/settings.json). db_path is used to resolve repo_root to
    # make the check independent of the caller's CWD.
    db_path = argv[0]
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))
    return check(repo_root)


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk review-check-perms", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
