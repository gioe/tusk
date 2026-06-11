#!/usr/bin/env python3
"""Mid-task friction notes consumed by /retro.

Captures one-line observations at the moment of friction — confusing tool
output, a workaround taken, a missing skill — instead of relying on hours-
old conversation memory at retro time. /retro reads jots for the parent
/tusk skill_run before doing its own analysis (issue #541).

Two subcommands share this script. The bin/tusk dispatcher routes:

    tusk jot <category> "<note>" [--file <path>] [--skill <name>]
        → tusk-jot.py write <category> <note> [--file ...] [--skill ...]

    tusk jots [--skill-run-id <id>] [--task-id <id>] [--limit N]
        → tusk-jot.py list [--skill-run-id ...] [--task-id ...] [--limit N]

`write` resolves the currently-active skill_run via the most-recent row
with ended_at IS NULL; the jot's task_id is copied from that row so the
retro reader can filter by either run or task.

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (reserved)
    sys.argv[3:] — mode ("write" | "list") + flags

Output: compact JSON (Convention 32). One row on `write`, an array on `list`.

Exit codes:
    0 — success
    1 — invalid input (no active skill_run on write, empty category/note)
    2 — argparse usage error
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py and tusk-json-lib.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection


def resolve_active_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Return the most-recent open skill_runs row, or None if none open."""
    return conn.execute(
        "SELECT id, task_id FROM skill_runs "
        "WHERE ended_at IS NULL "
        "ORDER BY started_at DESC LIMIT 1"
    ).fetchone()


def write_jot(
    conn: sqlite3.Connection,
    *,
    category: str,
    note: str,
    file_hint: str | None,
    skill_hint: str | None,
) -> dict:
    """Insert one jots row keyed to the active skill_run.

    Raises ValueError when no skill_run is currently open — the caller
    surfaces it as exit 1 with a recovery hint.
    """
    active = resolve_active_run(conn)
    if active is None:
        raise ValueError(
            "No active skill_run — start one with "
            "'tusk skill-run start <skill_name>' or 'tusk task-start <id> --skill <name>' first"
        )

    cursor = conn.execute(
        "INSERT INTO jots "
        "  (skill_run_id, task_id, category, note, file_hint, skill_hint) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (active["id"], active["task_id"], category, note, file_hint, skill_hint),
    )
    conn.commit()
    new_id = cursor.lastrowid

    row = conn.execute(
        "SELECT id, skill_run_id, task_id, category, note, "
        "       file_hint, skill_hint, created_at "
        "  FROM jots WHERE id = ?",
        (new_id,),
    ).fetchone()
    return dict(row)


def list_jots(
    conn: sqlite3.Connection,
    *,
    skill_run_id: int | None,
    task_id: int | None,
    limit: int,
) -> list[dict]:
    """Return jots filtered by skill_run_id, task_id, or both.

    Newest-first; no filter returns the most-recent `limit` rows globally
    (useful for ad-hoc inspection — /retro should always pass at least
    one filter).
    """
    where_clauses = []
    params: list = []
    if skill_run_id is not None:
        where_clauses.append("skill_run_id = ?")
        params.append(skill_run_id)
    if task_id is not None:
        where_clauses.append("task_id = ?")
        params.append(task_id)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    params.append(limit)

    rows = conn.execute(
        "SELECT id, skill_run_id, task_id, category, note, "
        "       file_hint, skill_hint, created_at "
        f"  FROM jots {where_sql} "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def main(argv: list) -> int:
    db_path = argv[0]
    # argv[1] is config_path — reserved
    parser = argparse.ArgumentParser(allow_abbrev=False,
        prog="tusk jot",
        description="Mid-task friction notes consumed by /retro.",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    w = sub.add_parser(
        "write", allow_abbrev=False,
        help="Insert one jot keyed to the currently-active skill_run.",
    )
    w.add_argument("category")
    w.add_argument("note")
    w.add_argument("--file", dest="file_hint", default=None,
                   help="Optional file path the jot is about (pre-classify hint).")
    w.add_argument("--skill", dest="skill_hint", default=None,
                   help="Optional skill name the jot is about (pre-classify hint).")

    ls = sub.add_parser(
        "list", allow_abbrev=False,
        help="List jots filtered by skill_run_id and/or task_id.",
    )
    ls.add_argument("--skill-run-id", type=int, default=None)
    ls.add_argument("--task-id", type=int, default=None)
    ls.add_argument("--limit", type=int, default=100)

    args = parser.parse_args(argv[2:])

    conn = get_connection(db_path)
    try:
        if args.mode == "write":
            if not args.category.strip():
                print("category must not be empty", file=sys.stderr)
                return 1
            if not args.note.strip():
                print("note must not be empty", file=sys.stderr)
                return 1
            try:
                row = write_jot(
                    conn,
                    category=args.category,
                    note=args.note,
                    file_hint=args.file_hint,
                    skill_hint=args.skill_hint,
                )
            except ValueError as e:
                print(str(e), file=sys.stderr)
                return 1
            print(dumps(row))
            return 0

        if args.mode == "list":
            rows = list_jots(
                conn,
                skill_run_id=args.skill_run_id,
                task_id=args.task_id,
                limit=args.limit,
            )
            print(dumps(rows))
            return 0
    finally:
        conn.close()

    return 2


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk jot <category> \"<note>\" [--file <path>] [--skill <name>]", file=sys.stderr)
        print("     tusk jots [--skill-run-id <id>] [--task-id <id>] [--limit N]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
