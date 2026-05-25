#!/usr/bin/env python3
"""Resolve a worktree-local tusk binary that supports the DB's schema version.

Invoked by ``bin/tusk``'s ``preflight_schema_version`` when the calling
binary's sibling ``tusk-migrate.py`` is behind ``PRAGMA user_version``.
Scans candidate binary paths inside every worktree returned by
``git worktree list --porcelain`` (rooted at the DB's repo) and returns the
first one whose sibling ``tusk-migrate.py`` knows about the higher version.

Generalizes the issue #866 fallback from ``bin/tusk-merge.py`` to every
PATH-invoked tusk subcommand (issue #876). Where
``_resolve_stable_tusk_bin`` *prefers primary and falls back to the caller's
worktree*, this helper *takes the caller as known-stale and searches sibling
worktrees for a compatible candidate* — inverse direction, different surface.

Usage::

    tusk-resolve-schema-bin.py <db_path> <caller_bin>

On match: prints the chosen binary path to stdout, emits a single-line
stderr diagnostic naming the redirect, and exits 0.
On miss / any error: exits 1 with no stdout.
"""

import os
import re
import sqlite3
import subprocess
import sys


_MIGRATE_REGISTRY_RE = re.compile(r"^\s*\((\d+),\s*migrate_\d+\)", re.MULTILINE)


def _bin_supports_schema(tusk_bin: str, required_version: int) -> bool:
    """Mirror bin/tusk's preflight grep: True when sibling tusk-migrate.py's
    MIGRATIONS registry advertises >= required_version, or when no parseable
    registry is present (bash returns 0 in that case too).
    """
    migrate_py = os.path.join(os.path.dirname(tusk_bin), "tusk-migrate.py")
    if not os.path.isfile(migrate_py):
        return True
    try:
        with open(migrate_py, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return True
    versions = [int(m) for m in _MIGRATE_REGISTRY_RE.findall(text)]
    if not versions:
        return True
    return max(versions) >= required_version


def _db_user_version(db_path: str) -> int | None:
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    return int(row[0])


def _worktree_paths(start: str) -> list[str]:
    """Return absolute worktree paths from ``git worktree list --porcelain``,
    scoped to the repo that contains ``start``. Empty list on any git error
    or when ``start`` is not inside a git repo."""
    try:
        result = subprocess.run(
            ["git", "-C", start, "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except (OSError, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            paths.append(line[len("worktree "):].strip())
    return paths


def _candidate_bins(worktree: str) -> list[str]:
    """Probe order inside a worktree. Source-repo's ``bin/tusk`` comes first
    because it's the source of truth in dev checkouts; consumer-project
    layouts (Claude / Codex) follow."""
    return [
        os.path.join(worktree, "bin", "tusk"),
        os.path.join(worktree, ".claude", "bin", "tusk"),
        os.path.join(worktree, "tusk", "bin", "tusk"),
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            f"usage: {os.path.basename(argv[0])} <db_path> <caller_bin>",
            file=sys.stderr,
        )
        return 2
    db_path, caller_bin = argv[1], argv[2]
    required = _db_user_version(db_path)
    if required is None:
        return 1

    caller_real = (
        os.path.realpath(caller_bin) if os.path.exists(caller_bin) else caller_bin
    )
    # DB lives at <repo_root>/tusk/tasks.db — walk two levels up to scope
    # the worktree scan to the right git repo regardless of CWD.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))
    worktrees = _worktree_paths(repo_root)
    if not worktrees:
        return 1

    for wt in worktrees:
        for candidate in _candidate_bins(wt):
            if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
                continue
            if os.path.realpath(candidate) == caller_real:
                continue
            if not _bin_supports_schema(candidate, required):
                continue
            print(
                f"tusk: this binary {caller_bin} does not support DB schema "
                f"v{required}; dispatching to worktree-local binary {candidate} "
                "(issue #876).",
                file=sys.stderr,
            )
            print(candidate)
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
