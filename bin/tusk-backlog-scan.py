#!/usr/bin/env python3
"""Consolidated backlog pre-flight scan for grooming sessions.

Called by the tusk wrapper:
    tusk backlog-scan [--duplicates] [--unassigned] [--unsized] [--expired]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — optional category flags

With no flags, returns all four categories. Individual flags scope the output
to that category only. Multiple flags are allowed.

Output JSON shape:
    {
        "duplicates": [{"task_a": {"id": N, "summary": "..."}, "task_b": {...}, "similarity": 0.N}, ...],
        "unassigned":  [{"id": N, "summary": "...", "domain": "..."}, ...],
        "unsized":     [{"id": N, "summary": "...", "domain": "...", "task_type": "..."}, ...],
        "expired":     [{"id": N, "summary": "...", "expires_at": "..."}, ...]
    }

Only the requested categories appear in the output object.
"""

import json
import os
import sqlite3
import subprocess
import sys


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def scan_expired(conn: sqlite3.Connection) -> list[dict]:
    """Open tasks past their expires_at date (supplements tusk autoclose)."""
    rows = conn.execute(
        "SELECT id, summary, expires_at FROM tasks "
        "WHERE status <> 'Done' "
        "  AND expires_at IS NOT NULL "
        "  AND expires_at < datetime('now') "
        "ORDER BY expires_at, id"
    ).fetchall()
    return [{"id": r["id"], "summary": r["summary"], "expires_at": r["expires_at"]} for r in rows]


def scan_unassigned(conn: sqlite3.Connection) -> list[dict]:
    """Open tasks with no assignee."""
    rows = conn.execute(
        "SELECT id, summary, domain FROM tasks "
        "WHERE status <> 'Done' "
        "  AND assignee IS NULL "
        "ORDER BY id"
    ).fetchall()
    return [{"id": r["id"], "summary": r["summary"], "domain": r["domain"]} for r in rows]


def scan_unsized(conn: sqlite3.Connection) -> list[dict]:
    """Open tasks with no complexity estimate."""
    rows = conn.execute(
        "SELECT id, summary, domain, task_type FROM tasks "
        "WHERE status <> 'Done' "
        "  AND complexity IS NULL "
        "ORDER BY id"
    ).fetchall()
    return [
        {"id": r["id"], "summary": r["summary"], "domain": r["domain"], "task_type": r["task_type"]}
        for r in rows
    ]


def scan_duplicates(db_path: str) -> list[dict]:
    """Call tusk dupes scan --status 'To Do' --json and return the duplicate_pairs list."""
    tusk_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk")
    result = subprocess.run(
        [tusk_bin, "dupes", "scan", "--status", "To Do", "--json"],
        capture_output=True,
        text=True,
    )
    # exit 0 = no pairs, 1 = pairs found, anything else = error
    if result.returncode not in (0, 1):
        return []
    try:
        data = json.loads(result.stdout)
        return data.get("duplicate_pairs", [])
    except (json.JSONDecodeError, KeyError):
        return []


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: tusk backlog-scan [--duplicates] [--unassigned] [--unsized] [--expired]",
              file=sys.stderr)
        return 1

    db_path = argv[0]
    # argv[1] is config_path — reserved for future use
    flags = set(argv[2:])

    if "--help" in flags:
        print("Usage: tusk backlog-scan [--duplicates] [--unassigned] [--unsized] [--expired]")
        print()
        print("Consolidated backlog pre-flight scan for grooming sessions.")
        print("No flags: returns all four categories.")
        print("  --duplicates  Heuristic duplicate pairs among open tasks")
        print("  --unassigned  Open tasks with no assignee")
        print("  --unsized     Open tasks with no complexity estimate")
        print("  --expired     Open tasks past their expires_at date")
        return 0

    known_flags = {"--duplicates", "--unassigned", "--unsized", "--expired"}
    unknown = flags - known_flags
    if unknown:
        print(f"Unknown flags: {' '.join(sorted(unknown))}", file=sys.stderr)
        print("Usage: tusk backlog-scan [--duplicates] [--unassigned] [--unsized] [--expired]",
              file=sys.stderr)
        return 1

    any_flag = bool(flags)
    want_duplicates = not any_flag or "--duplicates" in flags
    want_unassigned = not any_flag or "--unassigned" in flags
    want_unsized    = not any_flag or "--unsized"    in flags
    want_expired    = not any_flag or "--expired"    in flags

    conn = get_connection(db_path)
    try:
        result: dict = {}

        if want_expired:
            result["expired"] = scan_expired(conn)

        if want_unassigned:
            result["unassigned"] = scan_unassigned(conn)

        if want_unsized:
            result["unsized"] = scan_unsized(conn)

        if want_duplicates:
            result["duplicates"] = scan_duplicates(db_path)

        print(json.dumps(result, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
