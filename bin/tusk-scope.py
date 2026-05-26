#!/usr/bin/env python3
"""Manage task scope — authoritative declarations supersede the
``task_referenced_paths`` hint cache.

Called by the tusk wrapper:
    tusk scope list <task_id>
    tusk scope add <task_id> <pattern> [--reason TEXT] [--source S]
    tusk scope lock <task_id> [--by NAME]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — subcommand + flags

Sources (CHECK constraint on ``task_scope.source``):
    auto_derived       — backfilled from task_referenced_paths
    operator_declared  — set via `tusk task-insert --scope <pattern>`
    creates            — set via `tusk task-insert --creates <path>`
    expanded_mid_task  — added by `tusk scope add` (default for this CLI)
    unbounded          — set via `tusk task-insert --unbounded`; signals
                         "no path restriction" to the commit-time scope
                         guard (scope-paths emits no patterns in that case)

Exit codes:
    0 — success (JSON payload on stdout)
    1 — usage error / task not found / DB error
    2 — validation error (bad --source)
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py and tusk-json-lib.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
get_connection = _db_lib.get_connection
dumps = _json_lib.dumps


VALID_SOURCES_ADD = ("expanded_mid_task", "operator_declared", "creates")


def _parse_task_id(raw: str) -> int:
    s = (raw or "").strip()
    if s.upper().startswith("TASK-"):
        s = s[5:]
    try:
        return int(s)
    except ValueError:
        print(f"Error: invalid task_id: {raw!r}", file=sys.stderr)
        sys.exit(1)


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def _ensure_task_exists(conn: sqlite3.Connection, task_id: int) -> None:
    row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        print(f"Error: task {task_id} not found", file=sys.stderr)
        sys.exit(1)


def _validate_pattern(pattern: str) -> "str | None":
    """Reject patterns the commit-time scope guard could never match.

    The guard does literal repo-root-relative string matching, so absolute
    paths and parent-traversal segments are noise rows that never enforce
    anything. Returning a non-None error string causes cmd_add to exit 2.
    """
    if pattern.startswith("/"):
        return f"Error: pattern must be a repo-root-relative path; got {pattern!r}"
    segments = pattern.split("/")
    if any(seg == ".." for seg in segments):
        return f"Error: pattern must not contain '..' segments; got {pattern!r}"
    return None


def cmd_list(args: argparse.Namespace, db_path: str) -> int:
    task_id = _parse_task_id(args.task_id)
    with get_connection(db_path) as conn:
        _ensure_task_exists(conn, task_id)
        rows = conn.execute(
            "SELECT id, task_id, pattern, source, reason, locked_at, locked_by, created_at "
            "FROM task_scope WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    print(dumps([_row_to_dict(r) for r in rows]))
    return 0


def cmd_add(args: argparse.Namespace, db_path: str) -> int:
    task_id = _parse_task_id(args.task_id)
    source = args.source
    if source not in VALID_SOURCES_ADD:
        joined = ", ".join(VALID_SOURCES_ADD)
        print(
            f"Error: invalid --source {source!r}. Valid for `scope add`: {joined}",
            file=sys.stderr,
        )
        return 2
    pattern = (args.pattern or "").strip()
    if not pattern:
        print("Error: <pattern> required", file=sys.stderr)
        return 1
    err = _validate_pattern(pattern)
    if err is not None:
        print(err, file=sys.stderr)
        return 2

    with get_connection(db_path) as conn:
        _ensure_task_exists(conn, task_id)
        conn.execute(
            "INSERT INTO task_scope (task_id, pattern, source, reason) "
            "VALUES (?, ?, ?, ?)",
            (task_id, pattern, source, args.reason),
        )
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        row = conn.execute(
            "SELECT id, task_id, pattern, source, reason, locked_at, locked_by, created_at "
            "FROM task_scope WHERE id = ?",
            (new_id,),
        ).fetchone()
    print(dumps(_row_to_dict(row)))
    return 0


def cmd_lock(args: argparse.Namespace, db_path: str) -> int:
    task_id = _parse_task_id(args.task_id)
    locked_by = args.by or os.environ.get("USER") or "unknown"
    with get_connection(db_path) as conn:
        _ensure_task_exists(conn, task_id)
        # Lock only rows that aren't already locked — re-running is a no-op
        # for previously-locked entries.
        cur = conn.execute(
            "UPDATE task_scope "
            "SET locked_at = datetime('now'), locked_by = ? "
            "WHERE task_id = ? AND locked_at IS NULL",
            (locked_by, task_id),
        )
        rows_locked = cur.rowcount
        conn.commit()
        locked_at_row = conn.execute(
            "SELECT MAX(locked_at) AS locked_at FROM task_scope WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    print(dumps({
        "task_id": task_id,
        "locked_at": locked_at_row["locked_at"],
        "locked_by": locked_by,
        "rows_locked": rows_locked,
    }))
    return 0


def main(argv: list) -> int:
    if len(argv) < 3:
        print(
            "Usage: tusk-scope.py <db_path> <config_path> <list|add|lock> ...",
            file=sys.stderr,
        )
        return 1

    db_path = argv[1]
    # config_path = argv[2]  # accepted for dispatcher-arity parity, unused

    parser = argparse.ArgumentParser(prog="tusk scope", description="Manage task scope")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List scope entries for a task")
    p_list.add_argument("task_id")

    p_add = sub.add_parser(
        "add",
        help="Add a scope pattern to a task (default source: expanded_mid_task)",
    )
    p_add.add_argument("task_id")
    p_add.add_argument("pattern")
    p_add.add_argument("--reason", default=None)
    p_add.add_argument(
        "--source",
        default="expanded_mid_task",
        choices=VALID_SOURCES_ADD,
    )

    p_lock = sub.add_parser(
        "lock",
        help="Stamp locked_at on every scope entry for a task",
    )
    p_lock.add_argument("task_id")
    p_lock.add_argument("--by", default=None, help="Lock attribution (defaults to $USER)")

    args = parser.parse_args(argv[3:])

    if args.cmd == "list":
        return cmd_list(args, db_path)
    if args.cmd == "add":
        return cmd_add(args, db_path)
    if args.cmd == "lock":
        return cmd_lock(args, db_path)

    parser.print_help(sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
