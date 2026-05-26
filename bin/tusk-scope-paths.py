#!/usr/bin/env python3
"""Print the referenced paths inferred from a task's summary/description/criteria.

Called by the tusk wrapper:
    tusk scope-paths <task_id>

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3] — task_id (integer or TASK-NNN prefix form)

Output:
    One repo-root-relative path per line on stdout. Empty output when the
    task has no scope signal (no referenced paths). Used by the pre-commit
    scope-guard hook to enforce that staged files fall within the inferred
    task scope.

When the scope set references a ``bin/tusk-*.py`` path that is not yet
tracked by git, the output is augmented with Rule-42's same-commit
companions (``bin/tusk``, ``MANIFEST``, ``.claude/tusk-manifest.json``)
so new-script tasks don't need ``TUSK_SCOPE_GUARD_BYPASS=1`` for the
dispatcher + manifest commit (issue #891).

Exit codes:
    0 — success (always, even when no paths)
    1 — error (bad arguments, task not found, DB issue)
"""

import fnmatch
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py, tusk-git-helpers.py

_db_lib = tusk_loader.load("tusk-db-lib")
_git_helpers = tusk_loader.load("tusk-git-helpers")
get_connection = _db_lib.get_connection
task_referenced_paths = _git_helpers.task_referenced_paths


_NEW_TUSK_SCRIPT_COMPANIONS = (
    "bin/tusk",
    "MANIFEST",
    ".claude/tusk-manifest.json",
)


def _repo_root_for_db(db_path: str) -> str:
    """Return the repo root for a tusk DB path.

    Tusk stores the DB at ``<repo>/tusk/tasks.db``, so the repo root is two
    levels up. Falls back to ``git rev-parse`` when the path layout is
    unconventional.
    """
    candidate = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))
    if os.path.isdir(os.path.join(candidate, ".git")) or os.path.isfile(
        os.path.join(candidate, ".git")
    ):
        return candidate
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=os.path.dirname(os.path.abspath(db_path)) or ".",
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except OSError:
        pass
    return candidate


def _untracked_tusk_scripts(paths: list, repo_root: str) -> list:
    """Return ``bin/tusk-*.py`` entries from ``paths`` that git does not
    currently track (untracked or absent — treated as new for Rule-42)."""
    candidates = [p for p in paths if fnmatch.fnmatchcase(p, "bin/tusk-*.py")]
    if not candidates:
        return []
    try:
        result = subprocess.run(
            ["git", "ls-files", "--", *candidates],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=repo_root,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    tracked = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return [c for c in candidates if c not in tracked]


def _parse_task_id(raw: str) -> int:
    s = (raw or "").strip()
    if s.upper().startswith("TASK-"):
        s = s[5:]
    try:
        return int(s)
    except ValueError:
        print(f"Error: invalid task_id: {raw!r}", file=sys.stderr)
        sys.exit(1)


def main(argv: list) -> int:
    if len(argv) != 4:
        print("Usage: tusk-scope-paths.py <db_path> <config_path> <task_id>", file=sys.stderr)
        return 1
    db_path = argv[1]
    # config_path = argv[2]  # unused — kept for dispatcher-arity parity
    task_id = _parse_task_id(argv[3])

    with get_connection(db_path) as conn:
        row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            print(f"Error: task {task_id} not found", file=sys.stderr)
            return 1
        # Prefer the authoritative task_scope table when any rows exist for
        # this task (TASK-471). When any row has source='unbounded', emit
        # nothing so the commit-time scope guard silently passes (the task
        # has been explicitly opted out of path restriction). Fall back to
        # the legacy task_referenced_paths hint cache only when task_scope
        # has no rows for this task — preserves behavior for tasks created
        # before migration 73 (scope_enforced=0) until an operator declares
        # scope explicitly via `tusk scope add` or recreates the task with
        # `tusk task-insert --scope/--creates/--unbounded`.
        scope_rows = conn.execute(
            "SELECT pattern, source FROM task_scope WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        if scope_rows:
            if any(r["source"] == "unbounded" for r in scope_rows):
                return 0
            seen: list = []
            seen_set: set = set()
            for r in scope_rows:
                pattern = r["pattern"]
                if pattern and pattern not in seen_set:
                    seen_set.add(pattern)
                    seen.append(pattern)
            paths = seen
        else:
            paths = list(task_referenced_paths(task_id, conn))

    # Rule-42 companion augmentation: when any referenced bin/tusk-*.py
    # path is not yet tracked by git, the same commit will need to touch
    # bin/tusk + MANIFEST + .claude/tusk-manifest.json (the dispatcher
    # case branch and the regenerated manifest) for lint Rules 8/18 to
    # pass. Union those companions in so new-script tasks don't need
    # TUSK_SCOPE_GUARD_BYPASS=1 for the choreography commit (issue #891).
    repo_root = _repo_root_for_db(db_path)
    if _untracked_tusk_scripts(paths, repo_root):
        path_set = set(paths)
        for companion in _NEW_TUSK_SCRIPT_COMPANIONS:
            if companion not in path_set:
                paths.append(companion)
                path_set.add(companion)

    for p in paths:
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
