#!/usr/bin/env python3
"""Manage objectives — initiative-level intent spanning one or more tasks.

Called by the tusk wrapper:
    tusk objective insert "<summary>" [--description <text>]
    tusk objective list [--status active|completed|abandoned|all] [--format json|text]
    tusk objective get <objective_id> [--format json|text]
    tusk objective update <objective_id> [--summary <s>] [--description <d>] [--status <s>]
    tusk objective link <objective_id> <task_id> [--type primary|contributes_to|follow_up]
    tusk objective unlink <objective_id> <task_id>
    tusk objective done <objective_id> --reason completed|abandoned

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (reserved)
    sys.argv[3:] — subcommand + flags

Output: compact JSON by default. `list`/`get --format text` emit a simple table.

The objectives and objective_tasks tables already exist (migration 77); this is
the producer surface that was never built. Closing an objective (done) sets its
own status/closed_at only and must NOT touch linked task rows — tasks remain the
independent shippable unit (see docs/DOMAIN.md).
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py, tusk-json-lib.py, tusk-git-helpers.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_git_helpers = tusk_loader.load("tusk-git-helpers")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
reject_shell_metacharacters = _git_helpers.reject_shell_metacharacters


STATUSES = ("active", "completed", "abandoned")
RELATIONSHIP_TYPES = ("primary", "contributes_to", "follow_up")
# `done` accepts only the terminal reasons; `update --status` allows reopening.
DONE_REASONS = ("completed", "abandoned")


def _parse_int_id(raw: str, *, prefix: str, label: str) -> int:
    """Parse an integer id, tolerating an optional ``PREFIX-`` form."""
    s = (raw or "").strip()
    if prefix and s.upper().startswith(prefix.upper()):
        s = s[len(prefix):]
    if not re.fullmatch(r"[0-9]+", s):
        raise ValueError(f"invalid {label}: {raw!r}")
    return int(s)


def _parse_objective_id(raw: str) -> int:
    return _parse_int_id(raw, prefix="OBJ-", label="objective_id")


def _parse_task_id(raw: str) -> int:
    return _parse_int_id(raw, prefix="TASK-", label="task_id")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def _ensure_objective_exists(conn: sqlite3.Connection, objective_id: int) -> None:
    row = conn.execute(
        "SELECT id FROM objectives WHERE id = ?", (objective_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"objective {objective_id} not found")


def _ensure_task_exists(conn: sqlite3.Connection, task_id: int) -> None:
    row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"task {task_id} not found")


def _fetch_objective(conn: sqlite3.Connection, objective_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, summary, description, status, created_at, updated_at, closed_at "
        "  FROM objectives WHERE id = ?",
        (objective_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"objective {objective_id} not found")
    return row


def _linked_tasks(conn: sqlite3.Connection, objective_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT t.id, t.summary, t.status, ot.relationship_type, ot.created_at "
        "  FROM objective_tasks ot "
        "  JOIN tasks t ON t.id = ot.task_id "
        " WHERE ot.objective_id = ? "
        " ORDER BY ot.relationship_type, t.id",
        (objective_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_insert(args: argparse.Namespace, conn: sqlite3.Connection) -> dict:
    summary = (args.summary or "").strip()
    if not summary:
        raise ValueError("summary must not be empty")
    description = args.description.strip() if args.description else None
    cursor = conn.execute(
        "INSERT INTO objectives (summary, description) VALUES (?, ?)",
        (summary, description),
    )
    conn.commit()
    return _row_to_dict(_fetch_objective(conn, int(cursor.lastrowid)))


def cmd_list(args: argparse.Namespace, conn: sqlite3.Connection) -> list[dict]:
    where = ""
    params: list = []
    if args.status != "all":
        where = "WHERE o.status = ?"
        params.append(args.status)
    rows = conn.execute(
        "SELECT o.id, o.summary, o.description, o.status, o.created_at, "
        "       o.updated_at, o.closed_at, "
        "       (SELECT COUNT(*) FROM objective_tasks ot WHERE ot.objective_id = o.id) "
        "         AS task_count "
        "  FROM objectives o "
        f" {where} "
        " ORDER BY o.status, o.id",
        params,
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def cmd_get(args: argparse.Namespace, conn: sqlite3.Connection) -> dict:
    objective_id = _parse_objective_id(args.objective_id)
    obj = _row_to_dict(_fetch_objective(conn, objective_id))
    obj["tasks"] = _linked_tasks(conn, objective_id)
    return obj


def cmd_update(args: argparse.Namespace, conn: sqlite3.Connection) -> dict:
    objective_id = _parse_objective_id(args.objective_id)
    _ensure_objective_exists(conn, objective_id)

    sets: list[str] = []
    params: list = []
    if args.summary is not None:
        summary = args.summary.strip()
        if not summary:
            raise ValueError("--summary must not be empty")
        sets.append("summary = ?")
        params.append(summary)
    if args.description is not None:
        sets.append("description = ?")
        params.append(args.description.strip() or None)
    if args.status is not None:
        sets.append("status = ?")
        params.append(args.status)
        # Keep closed_at consistent with the lifecycle: stamp it on the way to a
        # terminal state, clear it when reopened to active.
        if args.status == "active":
            sets.append("closed_at = NULL")
        else:
            sets.append("closed_at = datetime('now')")
    if not sets:
        raise ValueError("update requires at least one of --summary/--description/--status")

    sets.append("updated_at = datetime('now')")
    params.append(objective_id)
    conn.execute(f"UPDATE objectives SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    return _row_to_dict(_fetch_objective(conn, objective_id))


def cmd_link(args: argparse.Namespace, conn: sqlite3.Connection) -> dict:
    objective_id = _parse_objective_id(args.objective_id)
    task_id = _parse_task_id(args.task_id)
    _ensure_objective_exists(conn, objective_id)
    _ensure_task_exists(conn, task_id)
    # Re-linking an existing pair updates its relationship_type rather than
    # erroring on the (objective_id, task_id) primary key.
    conn.execute(
        "INSERT INTO objective_tasks (objective_id, task_id, relationship_type) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(objective_id, task_id) "
        "DO UPDATE SET relationship_type = excluded.relationship_type",
        (objective_id, task_id, args.relationship_type),
    )
    conn.commit()
    row = conn.execute(
        "SELECT objective_id, task_id, relationship_type, created_at "
        "  FROM objective_tasks WHERE objective_id = ? AND task_id = ?",
        (objective_id, task_id),
    ).fetchone()
    return _row_to_dict(row)


def cmd_unlink(args: argparse.Namespace, conn: sqlite3.Connection) -> dict:
    objective_id = _parse_objective_id(args.objective_id)
    task_id = _parse_task_id(args.task_id)
    _ensure_objective_exists(conn, objective_id)
    cursor = conn.execute(
        "DELETE FROM objective_tasks WHERE objective_id = ? AND task_id = ?",
        (objective_id, task_id),
    )
    conn.commit()
    return {
        "objective_id": objective_id,
        "task_id": task_id,
        "removed": cursor.rowcount > 0,
    }


def cmd_done(args: argparse.Namespace, conn: sqlite3.Connection) -> dict:
    objective_id = _parse_objective_id(args.objective_id)
    _ensure_objective_exists(conn, objective_id)
    # Closing an objective updates ONLY the objectives row. Linked tasks keep
    # their own status — tasks are the independent shippable unit.
    conn.execute(
        "UPDATE objectives "
        "   SET status = ?, closed_at = datetime('now'), updated_at = datetime('now') "
        " WHERE id = ?",
        (args.reason, objective_id),
    )
    conn.commit()
    return _row_to_dict(_fetch_objective(conn, objective_id))


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

def _format_list_text(rows: list[dict]) -> str:
    if not rows:
        return "No objectives found."
    lines = []
    for r in rows:
        summary = " ".join((r["summary"] or "").split())
        lines.append(
            f"OBJ-{r['id']}  [{r['status']}]  ({r['task_count']} task(s))  {summary}"
        )
    return "\n".join(lines)


def _format_get_text(obj: dict) -> str:
    summary = " ".join((obj["summary"] or "").split())
    lines = [
        f"OBJ-{obj['id']}  [{obj['status']}]  {summary}",
    ]
    if obj.get("description"):
        lines.append(f"  {' '.join(obj['description'].split())}")
    tasks = obj.get("tasks") or []
    if not tasks:
        lines.append("  (no linked tasks)")
    else:
        lines.append("  Linked tasks:")
        for t in tasks:
            tsummary = " ".join((t["summary"] or "").split())
            lines.append(
                f"    TASK-{t['id']}  [{t['status']}]  ({t['relationship_type']})  {tsummary}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _check_metacharacters(args: argparse.Namespace) -> str | None:
    """Reject shell-substitution metacharacters in operator/agent text args.

    Mirrors the issue #881/#1106/#1107 guard: zsh and bash expand `, $(...),
    ${...}, and bare $IDENT before tusk sees the argv, even inside double
    quotes, so summary/description would be silently corrupted. Returns a
    diagnostic string when a field is unsafe, else None.
    """
    fields = []
    if args.mode == "insert":
        fields = [("objective summary", args.summary),
                  ("objective description", args.description)]
    elif args.mode == "update":
        fields = [("objective summary", args.summary),
                  ("objective description", args.description)]
    for subject, value in fields:
        if value is None:
            continue
        ok, diagnostic = reject_shell_metacharacters(value, subject=subject)
        if not ok:
            return diagnostic
    return None


def main(argv: list[str]) -> int:
    db_path = argv[0]
    # argv[1] is config_path — reserved
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        prog="tusk objective",
        description="Manage objectives (initiative-level intent) and their task links.",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    ins = sub.add_parser("insert", allow_abbrev=False, help="Create an objective.")
    ins.add_argument("summary", help="One-line objective summary.")
    ins.add_argument("--description", default=None)

    ls = sub.add_parser("list", allow_abbrev=False, help="List objectives.")
    ls.add_argument("--status", choices=(*STATUSES, "all"), default="active")
    ls.add_argument("--format", choices=("json", "text"), default="json")

    get = sub.add_parser("get", allow_abbrev=False, help="Get one objective + linked tasks.")
    get.add_argument("objective_id", help="Objective ID, e.g. 7 or OBJ-7.")
    get.add_argument("--format", choices=("json", "text"), default="json")

    upd = sub.add_parser("update", allow_abbrev=False, help="Update an objective.")
    upd.add_argument("objective_id", help="Objective ID, e.g. 7 or OBJ-7.")
    upd.add_argument("--summary", default=None)
    upd.add_argument("--description", default=None)
    upd.add_argument("--status", choices=STATUSES, default=None)

    link = sub.add_parser("link", allow_abbrev=False, help="Link a task to an objective.")
    link.add_argument("objective_id", help="Objective ID, e.g. 7 or OBJ-7.")
    link.add_argument("task_id", help="Task ID, e.g. 42 or TASK-42.")
    link.add_argument("--type", dest="relationship_type",
                      choices=RELATIONSHIP_TYPES, default="contributes_to")

    unlink = sub.add_parser("unlink", allow_abbrev=False, help="Unlink a task from an objective.")
    unlink.add_argument("objective_id", help="Objective ID, e.g. 7 or OBJ-7.")
    unlink.add_argument("task_id", help="Task ID, e.g. 42 or TASK-42.")

    done = sub.add_parser("done", allow_abbrev=False, help="Close an objective.")
    done.add_argument("objective_id", help="Objective ID, e.g. 7 or OBJ-7.")
    done.add_argument("--reason", choices=DONE_REASONS, required=True)

    args = parser.parse_args(argv[2:])

    diagnostic = _check_metacharacters(args)
    if diagnostic is not None:
        print(diagnostic, file=sys.stderr)
        return 1

    conn = get_connection(db_path)
    try:
        try:
            if args.mode == "insert":
                print(dumps(cmd_insert(args, conn)))
                return 0
            if args.mode == "list":
                rows = cmd_list(args, conn)
                if args.format == "text":
                    print(_format_list_text(rows))
                else:
                    print(dumps(rows))
                return 0
            if args.mode == "get":
                obj = cmd_get(args, conn)
                if args.format == "text":
                    print(_format_get_text(obj))
                else:
                    print(dumps(obj))
                return 0
            if args.mode == "update":
                print(dumps(cmd_update(args, conn)))
                return 0
            if args.mode == "link":
                print(dumps(cmd_link(args, conn)))
                return 0
            if args.mode == "unlink":
                print(dumps(cmd_unlink(args, conn)))
                return 0
            if args.mode == "done":
                print(dumps(cmd_done(args, conn)))
                return 0
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except sqlite3.IntegrityError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    finally:
        conn.close()

    return 2


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk objective insert \"<summary>\" [--description <text>]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
