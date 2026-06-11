#!/usr/bin/env python3
"""Increment VERSION by 1, write, stage, and echo the new value.

Called by the tusk wrapper:
    tusk version-bump [--task-id <N>]

Arguments received from the tusk wrapper:
    sys.argv[1] — REPO_ROOT (CWD's .git walk-up)
    sys.argv[2] — DB_PATH
    sys.argv[3] — SCRIPT_DIR (bin/ containing this script)
    sys.argv[4] — INSTALL_DIR (install root, fallback for consumer installs)
    sys.argv[5:] — caller args

Resolution order for the VERSION file:
    1. If --task-id <N> is passed, look up task_workspaces.workspace_path for
       <N> and bump <workspace_path>/VERSION. This is the authoritative path
       when the operator is at the primary checkout (on the default branch)
       and wants the bump to land in the task's worktree (issue #903).
    2. Otherwise fall back to REPO_ROOT/VERSION (CWD's checkout — preserves
       the worktree-aware behavior from issues #798/#801 when invoked from
       inside the worktree).
    3. Finally fall back to SCRIPT_DIR/VERSION and INSTALL_DIR/VERSION for
       consumer projects whose REPO_ROOT contains no VERSION file.
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py

_db_lib = tusk_loader.load("tusk-db-lib")
resolve_task_workspace = _db_lib.resolve_task_workspace
maybe_advise_primary_no_task_id = _db_lib.maybe_advise_primary_no_task_id


def main() -> None:
    if len(sys.argv) < 5:
        print(
            "Usage: tusk version-bump [--task-id <N>]",
            file=sys.stderr,
        )
        sys.exit(1)
    repo_root = sys.argv[1]
    db_path = sys.argv[2]
    script_dir = sys.argv[3]
    install_dir = sys.argv[4]
    user_args = sys.argv[5:]

    parser = argparse.ArgumentParser(allow_abbrev=False,
        prog="tusk version-bump",
        description=(
            "Increment VERSION by 1, write, stage, and echo the new value. "
            "Use --task-id to bump a task worktree's VERSION from the primary checkout."
        ),
        usage="tusk version-bump [--task-id <N>]",
    )
    parser.add_argument(
        "--task-id",
        type=int,
        default=None,
        help="Resolve VERSION against the task's recorded workspace_path.",
    )
    parsed = parser.parse_args(user_args)

    if parsed.task_id is not None:
        target_root = resolve_task_workspace(db_path, parsed.task_id)
        version_file = os.path.join(target_root, "VERSION")
        if not os.path.isfile(version_file):
            print(
                f"Error: --task-id {parsed.task_id} workspace {target_root!r} "
                "has no VERSION file.",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        maybe_advise_primary_no_task_id(db_path, repo_root, command="tusk version-bump")
        target_root = repo_root
        candidates = [
            os.path.join(repo_root, "VERSION"),
            os.path.join(script_dir, "VERSION"),
            os.path.join(install_dir, "VERSION"),
        ]
        version_file = next((p for p in candidates if os.path.isfile(p)), None)
        if version_file is None:
            print("Error: VERSION file not found", file=sys.stderr)
            sys.exit(1)
        target_root = os.path.dirname(version_file)

    with open(version_file, encoding="utf-8") as f:
        current = f.read().strip()
    try:
        new_version = int(current) + 1
    except ValueError:
        print(
            f"Error: VERSION file content {current!r} is not an integer",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(version_file, "w", encoding="utf-8") as f:
        f.write(f"{new_version}\n")

    subprocess.run(
        ["git", "-C", target_root, "add", version_file],
        check=True,
        encoding="utf-8",
    )
    print(new_version)


if __name__ == "__main__":
    main()
