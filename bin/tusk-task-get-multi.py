#!/usr/bin/env python3
"""Read-only fetch of multiple task bundles in a single call.

Called by the tusk wrapper:
    tusk task-get-multi <id> [<id> ...]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (unused)
    sys.argv[3+] — one or more task IDs (integer or TASK-NNN form)

Returns a JSON array of task objects, each with the same fields as
tusk task-get (task row + acceptance_criteria + task_progress).
Unknown IDs are silently omitted. Order matches the input ID order.
Does not modify any state.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection


def _parse_id(value: str) -> int:
    v = value
    if v.upper().startswith("TASK-"):
        v = v[5:]
    return int(v)


def main(argv: list[str]) -> int:
    db_path = argv[0]
    # argv[1] is config_path (unused)
    raw_ids = argv[2:]

    if not raw_ids:
        print("Usage: tusk task-get-multi <id> [<id> ...]", file=sys.stderr)
        return 1

    try:
        task_ids = [_parse_id(v) for v in raw_ids]
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    conn = get_connection(db_path)
    try:
        placeholders = ",".join("?" for _ in task_ids)

        task_rows = conn.execute(
            f"SELECT * FROM tasks WHERE id IN ({placeholders})",
            task_ids,
        ).fetchall()

        # Index by id for fast lookup
        tasks_by_id = {row["id"]: row for row in task_rows}

        found_ids = list(tasks_by_id.keys())
        if not found_ids:
            print("[]")
            return 0

        criteria_rows = conn.execute(
            f"SELECT id, task_id, criterion, source, is_completed, "
            f"criterion_type, verification_spec, created_at, updated_at "
            f"FROM acceptance_criteria WHERE task_id IN ({placeholders}) ORDER BY id",
            task_ids,
        ).fetchall()

        progress_rows = conn.execute(
            f"SELECT * FROM task_progress WHERE task_id IN ({placeholders}) ORDER BY created_at DESC",
            task_ids,
        ).fetchall()

        # Group criteria and progress by task_id
        criteria_by_task: dict[int, list] = {tid: [] for tid in found_ids}
        for row in criteria_rows:
            tid = row["task_id"]
            if tid in criteria_by_task:
                criteria_by_task[tid].append({key: row[key] for key in row.keys()})

        progress_by_task: dict[int, list] = {tid: [] for tid in found_ids}
        for row in progress_rows:
            tid = row["task_id"]
            if tid in progress_by_task:
                progress_by_task[tid].append({key: row[key] for key in row.keys()})

        # Return results in input order, skipping unknown IDs
        results = []
        for tid in task_ids:
            if tid not in tasks_by_id:
                continue
            task_row = tasks_by_id[tid]
            results.append({
                "task": {key: task_row[key] for key in task_row.keys()},
                "acceptance_criteria": criteria_by_task.get(tid, []),
                "task_progress": progress_by_task.get(tid, []),
            })

        print(dumps(results))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-get-multi <id> [<id> ...]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
