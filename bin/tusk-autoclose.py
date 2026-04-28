#!/usr/bin/env python3
"""Consolidate groom-backlog auto-close pre-checks into a single CLI command.

Called by the tusk wrapper:
    tusk autoclose [--dry-run]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — flags (--dry-run)

Runs one pre-check:
  - Moot contingent tasks → closed_reason = 'wont_do'

For each closure, appends an annotation to the description and closes open sessions.
Prints a JSON summary with counts per category and closed task IDs.

With --dry-run, the SELECT halves run unchanged but the UPDATE side effects
(close_task / close_sessions) are skipped, so the same candidate IDs surface
without modifying the DB. The output shape is identical except `applied`
flips to false and `moot_details` is omitted.
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection


def close_sessions(conn: sqlite3.Connection, task_id: int) -> int:
    """Close all open sessions for a task. Returns number of sessions closed."""
    cursor = conn.execute(
        "UPDATE task_sessions "
        "SET ended_at = datetime('now'), "
        "    duration_seconds = CAST((julianday(datetime('now')) - julianday(started_at)) * 86400 AS INTEGER), "
        "    lines_added = COALESCE(lines_added, 0), "
        "    lines_removed = COALESCE(lines_removed, 0) "
        "WHERE task_id = ? AND ended_at IS NULL",
        (task_id,),
    )
    return cursor.rowcount


def close_task(conn: sqlite3.Connection, task_id: int, reason: str, annotation: str) -> None:
    """Set task to Done with closed_reason and append annotation to description."""
    conn.execute(
        "UPDATE tasks "
        "SET status = 'Done', "
        "    closed_reason = ?, "
        "    updated_at = datetime('now'), "
        "    description = description || char(10) || char(10) || '---' || char(10) || ? "
        "WHERE id = ?",
        (reason, annotation, task_id),
    )


def autoclose_moot_contingent(conn: sqlite3.Connection, dry_run: bool = False) -> list[dict]:
    """Close tasks contingent on upstream tasks that closed as wont_do/expired.
    Returns list of dicts with (would-be) closed task ID and upstream reference.

    With dry_run=True, runs only the SELECT and skips close_task / close_sessions
    so callers can preview candidates without mutating the DB.
    """
    rows = conn.execute(
        "SELECT t.id, t.summary, "
        "       d.depends_on_id AS upstream_id, "
        "       upstream.closed_reason AS upstream_reason "
        "FROM tasks t "
        "JOIN task_dependencies d ON t.id = d.task_id "
        "JOIN tasks upstream ON d.depends_on_id = upstream.id "
        "WHERE t.status <> 'Done' "
        "  AND d.relationship_type = 'contingent' "
        "  AND upstream.status = 'Done' "
        "  AND upstream.closed_reason IN ('wont_do', 'expired')"
    ).fetchall()

    candidates = [
        {
            "id": row["id"],
            "upstream_id": row["upstream_id"],
            "upstream_reason": row["upstream_reason"],
        }
        for row in rows
    ]
    if dry_run:
        return candidates

    for c in candidates:
        annotation = (
            f"Auto-closed: Contingent on TASK-{c['upstream_id']} "
            f"which closed as {c['upstream_reason']}."
        )
        close_task(conn, c["id"], "wont_do", annotation)
        close_sessions(conn, c["id"])

    return candidates


USAGE = "Usage: tusk autoclose [--dry-run]"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(USAGE, file=sys.stderr)
        return 1

    db_path = argv[0]
    # argv[1] is config_path — reserved for future use
    flags = argv[2:]

    known_flags = {"--dry-run"}
    unknown = [f for f in flags if f not in known_flags]
    if unknown:
        print(f"Unknown flags: {' '.join(unknown)}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 1

    dry_run = "--dry-run" in flags

    conn = get_connection(db_path)
    try:
        moot_closed = autoclose_moot_contingent(conn, dry_run=dry_run)

        if not dry_run:
            conn.commit()

        summary = {
            "applied": not dry_run,
            "moot_contingent": {"count": len(moot_closed), "task_ids": [c["id"] for c in moot_closed]},
            "total_closed": len(moot_closed),
        }

        if not dry_run and moot_closed:
            summary["moot_details"] = moot_closed

        print(dumps(summary))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
