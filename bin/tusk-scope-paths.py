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

Exit codes:
    0 — success (always, even when no paths)
    1 — error (bad arguments, task not found, DB issue)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py, tusk-git-helpers.py

_db_lib = tusk_loader.load("tusk-db-lib")
_git_helpers = tusk_loader.load("tusk-git-helpers")
get_connection = _db_lib.get_connection
task_referenced_paths = _git_helpers.task_referenced_paths


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
        for p in task_referenced_paths(task_id, conn):
            print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
