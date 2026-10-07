#!/usr/bin/env python3
"""Mid-task friction notes consumed by /retro.

Captures one-line observations at the moment of friction — confusing tool
output, a workaround taken, a missing skill — instead of relying on hours-
old conversation memory at retro time. /retro reads jots for the parent
/tusk skill_run before doing its own analysis (issue #541).

Capture, listing, and triage share this script. The bin/tusk dispatcher routes:

    tusk jot write <category> "<note>" [--file <path>] [--skill <name>]
        [--task-id <id>] [--skill-run-id <id>]
    tusk jot <category> "<note>" [--file <path>] [--skill <name>]
        [--task-id <id>] [--skill-run-id <id>]  # shorthand
        → tusk-jot.py write <category> <note> [--file ...] [--skill ...]

    tusk jot promote <jot-id> --to <existing-provenance-ref>
    tusk jot dismiss <jot-id> --reason "<reason>"
    tusk jots [--skill-run-id <id>] [--task-id <id>] [--limit N]
        [--triage-status pending|promoted|dismissed|all]
        → tusk-jot.py list [--skill-run-id ...] [--task-id ...] [--limit N]

`write` accepts an explicit skill-run or task identity. Without one, it uses
the caller's recorded task workspace when available. A single globally-open
run remains the compatibility fallback; ambiguous targets are rejected. The
jot's task_id is copied from the selected run so the retro reader can filter
by either run or task.

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
import tusk_loader  # loads tusk-db-lib.py, tusk-json-lib.py, tusk-git-helpers.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_git_helpers = tusk_loader.load("tusk-git-helpers")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
reject_shell_metacharacters = _git_helpers.reject_shell_metacharacters


def _open_runs(
    conn: sqlite3.Connection, *, task_id: int | None = None
) -> list[sqlite3.Row]:
    where = "WHERE ended_at IS NULL"
    params: tuple = ()
    if task_id is not None:
        where += " AND task_id = ?"
        params = (task_id,)
    return conn.execute(
        "SELECT id, task_id FROM skill_runs "
        f"{where} ORDER BY started_at DESC, id DESC",
        params,
    ).fetchall()


def _workspace_task_id(
    conn: sqlite3.Connection, caller_root: str | None
) -> int | None:
    if not caller_root:
        return None
    caller = os.path.realpath(caller_root)
    rows = conn.execute(
        "SELECT task_id, workspace_path FROM task_workspaces"
    ).fetchall()
    for row in rows:
        path = row["workspace_path"]
        if path and os.path.realpath(path) == caller:
            return row["task_id"]
    return None


def _require_unique_open_run(
    conn: sqlite3.Connection, task_id: int
) -> sqlite3.Row:
    rows = _open_runs(conn, task_id=task_id)
    if not rows:
        raise ValueError(
            f"No open skill_run for task {task_id} — start one with "
            f"'tusk task-start {task_id} --skill <name>' first"
        )
    if len(rows) > 1:
        ids = ", ".join(str(row["id"]) for row in rows)
        raise ValueError(
            f"Ambiguous jot target: task {task_id} has multiple open skill runs "
            f"({ids}). Re-run with --skill-run-id <id>."
        )
    return rows[0]


def resolve_active_run(
    conn: sqlite3.Connection,
    *,
    skill_run_id: int | None = None,
    task_id: int | None = None,
    caller_root: str | None = None,
) -> sqlite3.Row | None:
    """Resolve one open run from explicit identity, caller workspace, or fallback."""
    if skill_run_id is not None:
        row = conn.execute(
            "SELECT id, task_id, ended_at FROM skill_runs WHERE id = ?",
            (skill_run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Skill run {skill_run_id} does not exist.")
        if row["ended_at"] is not None:
            raise ValueError(
                f"Skill run {skill_run_id} is closed; select an open skill run."
            )
        if task_id is not None and row["task_id"] != task_id:
            raise ValueError(
                f"Skill run {skill_run_id} belongs to task {row['task_id']}, "
                f"not requested task {task_id}."
            )
        return row

    if task_id is not None:
        return _require_unique_open_run(conn, task_id)

    workspace_task_id = _workspace_task_id(conn, caller_root)
    if workspace_task_id is not None:
        return _require_unique_open_run(conn, workspace_task_id)

    rows = _open_runs(conn)
    if not rows:
        return None
    if len(rows) > 1:
        targets = ", ".join(
            f"{row['id']} (task {row['task_id']})" for row in rows
        )
        raise ValueError(
            "Ambiguous jot target: multiple skill runs are open "
            f"({targets}) and the caller is not a recorded task workspace. "
            "Re-run with --skill-run-id <id> or --task-id <id>."
        )
    return rows[0]


def write_jot(
    conn: sqlite3.Connection,
    *,
    category: str,
    note: str,
    file_hint: str | None,
    skill_hint: str | None,
    skill_run_id: int | None = None,
    task_id: int | None = None,
    caller_root: str | None = None,
) -> dict:
    """Insert one jots row keyed to the active skill_run.

    Raises ValueError when no skill_run is currently open — the caller
    surfaces it as exit 1 with a recovery hint.
    """
    active = resolve_active_run(
        conn,
        skill_run_id=skill_run_id,
        task_id=task_id,
        caller_root=caller_root,
    )
    if active is None:
        raise ValueError(
            "No active skill_run — start one with "
            "'tusk skill-run start <skill_name>' or 'tusk task-start <id> --skill <name>' first"
        )

    new_id = tusk_loader.load("tusk-observation-lib").insert_observation(
        conn, skill_run_id=active["id"], task_id=active["task_id"],
        category=category, note=note, file_hint=file_hint, skill_hint=skill_hint,
    )

    row = conn.execute(
        "SELECT id, skill_run_id, task_id, category, note, "
        "       file_hint, skill_hint, created_at "
        "  FROM jots WHERE id = ?",
        (new_id,),
    ).fetchone()
    result = dict(row)
    conn.commit()
    return result


def list_jots(
    conn: sqlite3.Connection,
    *,
    skill_run_id: int | None,
    task_id: int | None,
    limit: int,
    triage_status: str | None = None,
) -> list[dict]:
    """Return jots filtered by skill_run_id, task_id, or both.

    Newest-first; no filter returns the most-recent `limit` rows globally
    (useful for ad-hoc inspection — /retro should always pass at least
    one filter).
    """
    if triage_status is not None:
        clauses, params = [], []
        for column, value in (("skill_run_id", skill_run_id), ("task_id", task_id)):
            if value is not None:
                clauses.append(f"c.{column}=?")
                params.append(value)
        if triage_status != "all":
            clauses.append("c.triage_status=?")
            params.append(triage_status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            "SELECT j.*,c.id AS context_id,c.triage_status FROM jots j "
            "JOIN jot_aliases a ON a.id=j.id JOIN task_context_items c ON c.id=a.context_id"
            + where + " ORDER BY j.created_at DESC,j.id DESC LIMIT ?", (*params, limit)).fetchall()
        triage = tusk_loader.load("tusk-observation-triage")
        return [dict(row, disposition=triage.details(conn, row["context_id"])) for row in rows]
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
    parser.add_argument("--caller-root", default=None, help=argparse.SUPPRESS)
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
    w.add_argument("--task-id", type=int, default=None,
                   help="Target the unique open skill run for this task.")
    w.add_argument("--skill-run-id", type=int, default=None,
                   help="Target this exact open skill run.")

    ls = sub.add_parser(
        "list", allow_abbrev=False,
        help="List jots filtered by skill_run_id and/or task_id.",
    )
    ls.add_argument("--skill-run-id", type=int, default=None)
    ls.add_argument("--task-id", type=int, default=None)
    ls.add_argument("--limit", type=int, default=100)

    ls.add_argument("--triage-status", choices=("pending", "promoted", "dismissed", "all"))
    promote = sub.add_parser("promote", allow_abbrev=False, help="Bind an existing decision/risk, criterion, or task outcome.")
    promote.add_argument("jot_id", type=int)
    promote.add_argument("--to", required=True)
    dismiss = sub.add_parser("dismiss", allow_abbrev=False, help="Dismiss an observation with a reason.")
    dismiss.add_argument("jot_id", type=int)
    dismiss.add_argument("--reason", required=True)

    if argv[2:] in (["--help"], ["-h"]):
        parser.print_help()
        print()
        w.print_help()  # Keep the capture syntax visible alongside triage.
        return 0
    args = parser.parse_args(argv[2:])

    conn = get_connection(db_path)
    try:
        if args.mode in ("promote", "dismiss"):
            try:
                reason = args.reason if args.mode == "dismiss" else None
                if reason is not None:
                    ok, diagnostic = reject_shell_metacharacters(reason, subject="dismissal reason")
                    if not ok:
                        raise ValueError(diagnostic)
                result = tusk_loader.load("tusk-observation-triage").apply(
                    conn, args.jot_id, destination=args.to if args.mode == "promote" else None, reason=reason)
                print(dumps(result))
                return 0
            except (ValueError, sqlite3.IntegrityError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
        if args.mode == "write":
            if not args.category.strip():
                print("category must not be empty", file=sys.stderr)
                return 1
            if not args.note.strip():
                print("note must not be empty", file=sys.stderr)
                return 1
            # Reject shell-substitution metacharacters in the category and note
            # before any DB write (issue #1107 wired the note; issue #1108 closed
            # the sibling category gap). zsh/bash expand `, $(...), ${...}, and
            # bare $IDENT before tusk sees the argv, even inside double quotes,
            # silently corrupting the stored jot. The category is a short label
            # that should never carry shell metacharacters, so it is guarded too.
            ok, diagnostic = reject_shell_metacharacters(
                args.category, subject="jot category")
            if not ok:
                print(diagnostic, file=sys.stderr)
                return 1
            ok, diagnostic = reject_shell_metacharacters(args.note, subject="jot note")
            if not ok:
                print(diagnostic, file=sys.stderr)
                return 1
            try:
                row = write_jot(
                    conn,
                    category=args.category,
                    note=args.note,
                    file_hint=args.file_hint,
                    skill_hint=args.skill_hint,
                    skill_run_id=args.skill_run_id,
                    task_id=args.task_id,
                    caller_root=args.caller_root,
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
                triage_status=args.triage_status,
            )
            print(dumps(rows))
            return 0
    finally:
        conn.close()

    return 2


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk jot write <category> \"<note>\" [--file <path>] [--skill <name>] [--task-id <id>] [--skill-run-id <id>]", file=sys.stderr)
        print("     tusk jot <category> \"<note>\" [--file <path>] [--skill <name>] [--task-id <id>] [--skill-run-id <id>]  # shorthand", file=sys.stderr)
        print("     tusk jots [--skill-run-id <id>] [--task-id <id>] [--limit N]", file=sys.stderr)
        sys.exit(1)
    # Retry the whole command (a fresh connection per attempt) on transient
    # "database is locked" contention under parallel worktree sessions (#1143).
    sys.exit(_db_lib.retry_on_locked(lambda: main(sys.argv[1:]), label="jot"))
