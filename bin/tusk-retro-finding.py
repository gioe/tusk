#!/usr/bin/env python3
"""Record one retro_findings row — the write side of cross-retro theme detection.

Called by the tusk wrapper:
    tusk retro-finding add --skill-run-id N --category X --summary Y [--task-id N] [--action-taken Z]

Replaces the raw-SQL insert pattern /retro previously used in skills/retro/SKILL.md
LR-3a and skills/retro/FULL-RETRO.md 6a. The wrapper eliminates the NULL-vs-
quoted-string footgun for task_id (omitting the flag yields a real NULL) and
validates skill_run_id / task_id as real FKs before the INSERT so dangling-FK
rows never land.

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (reserved for future use)
    sys.argv[3:] — subcommand + flags

Flags (for `add`):
    --skill-run-id N    Required. Must reference an existing skill_runs.id.
    --category X        Required. Non-empty; intentionally free-text so custom
                        FOCUS.md category labels flow through unchanged.
    --summary Y         Required. Non-empty; the one-line finding description.
    --task-id N         Optional. If supplied, must reference tasks.id. Omit
                        the flag (do NOT pass an empty string) to store NULL.
    --action-taken Z    Optional. One of the vocabulary tokens documented in
                        skills/retro/SKILL.md LR-3a / FULL-RETRO.md 6a, or
                        omitted to store NULL.

Output (on success): JSON describing the inserted row:
    {"id": N, "skill_run_id": N, "task_id": N|null, "category": "...",
     "summary": "...", "action_taken": "..."|null, "created_at": "..."}

Exit codes:
    0 — row inserted
    1 — invalid input (unknown skill_run_id, unknown task_id, empty required field)
    2 — argparse usage error (missing required flag, unknown subcommand)
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # noqa: E402

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection


def add_finding(
    conn: sqlite3.Connection,
    *,
    skill_run_id: int,
    category: str,
    summary: str,
    task_id: int | None = None,
    action_taken: str | None = None,
) -> dict:
    """Insert one retro_findings row after validating FK references.

    Raises ValueError on unknown skill_run_id or unknown task_id; the CLI
    surfaces both as exit-1 errors with a human-readable stderr message.
    Category and summary must be non-empty after strip() — callers enforce
    this before invoking to keep the DB free of whitespace-only rows.
    """
    if not conn.execute(
        "SELECT 1 FROM skill_runs WHERE id = ?", (skill_run_id,)
    ).fetchone():
        raise ValueError(f"skill_run_id {skill_run_id} does not exist")
    if task_id is not None and not conn.execute(
        "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
    ).fetchone():
        raise ValueError(f"task_id {task_id} does not exist")

    cursor = conn.execute(
        "INSERT INTO retro_findings "
        "  (skill_run_id, task_id, category, summary, action_taken) "
        "VALUES (?, ?, ?, ?, ?)",
        (skill_run_id, task_id, category, summary, action_taken),
    )
    conn.commit()
    new_id = cursor.lastrowid

    row = conn.execute(
        "SELECT id, skill_run_id, task_id, category, summary, action_taken, "
        "       created_at "
        "  FROM retro_findings WHERE id = ?",
        (new_id,),
    ).fetchone()
    return dict(row)


def main(argv: list) -> int:
    db_path = argv[0]
    # argv[1] is config_path — reserved for future use
    parser = argparse.ArgumentParser(
        prog="tusk retro-finding",
        description=(
            "Record a retro_findings row — the write side of cross-retro "
            "theme detection. Wraps the INSERT /retro previously ran inline "
            "so that skill_run_id is FK-checked and task_id NULL handling "
            "is not string-templated."
        ),
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    add_p = subparsers.add_parser(
        "add",
        help="Insert one retro_findings row after validating FK references.",
    )
    add_p.add_argument("--skill-run-id", type=int, required=True)
    add_p.add_argument("--task-id", type=int, default=None)
    add_p.add_argument("--category", required=True)
    add_p.add_argument("--summary", required=True)
    add_p.add_argument("--action-taken", default=None)
    args = parser.parse_args(argv[2:])

    if args.action == "add":
        if not args.category.strip():
            print("--category must not be empty", file=sys.stderr)
            return 1
        if not args.summary.strip():
            print("--summary must not be empty", file=sys.stderr)
            return 1

        conn = get_connection(db_path)
        try:
            try:
                row = add_finding(
                    conn,
                    skill_run_id=args.skill_run_id,
                    task_id=args.task_id,
                    category=args.category,
                    summary=args.summary,
                    action_taken=args.action_taken,
                )
            except ValueError as e:
                print(str(e), file=sys.stderr)
                return 1
            print(dumps(row))
            return 0
        finally:
            conn.close()

    return 2


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print(
            "Use: tusk retro-finding add --skill-run-id N --category X --summary Y ...",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
