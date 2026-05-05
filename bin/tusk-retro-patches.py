#!/usr/bin/env python3
"""List skill-patch retro_findings for follow-up validation, emitted as JSON.

`/retro` LR-2a applies inline patches to skill files or `CLAUDE.md` and
records them in `retro_findings` with `action_taken = skill-patch:<file>`.
Once applied, nothing today checks whether the patch worked. This command
surfaces those patches so a future SessionStart hook (or a human review)
can close the loop — issue #540.

Confirmation convention: a later `retro_findings` row with
`action_taken = skill-patch-confirmed:<same file>` and
`created_at > <patch row>.created_at` marks the original patch confirmed.
The `--unconfirmed` flag filters to patches lacking such a follow-up.

Called by the tusk wrapper:
    tusk retro-patches [--window-days N] [--unconfirmed]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path

Flags:
    --window-days N   Look-back window for `created_at`. Default 30.
                      0 means "all history" (no window filter).
    --unconfirmed     Exclude patches that already have a follow-up
                      `skill-patch-confirmed:<file>` row.

Output JSON shape (newest-first):
    [
        {
            "finding_id": N,
            "skill_run_id": N,
            "task_id": N | null,
            "action_taken": "skill-patch:<file>",
            "target_file": "<file>",
            "created_at": "YYYY-MM-DD HH:MM:SS",
            "age_days": N
        },
        ...
    ]

Exit codes:
    0 — success
    1 — error (bad arguments, DB issue)
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


DEFAULT_WINDOW_DAYS = 30
PATCH_PREFIX = "skill-patch:"
CONFIRMED_PREFIX = "skill-patch-confirmed:"


def fetch_patches(
    conn: sqlite3.Connection,
    *,
    window_days: int,
    unconfirmed_only: bool,
) -> list:
    """Return skill-patch findings as a list of dicts, newest-first.

    - window_days == 0 disables the date filter (all history).
    - window_days > 0 limits to rows whose created_at >= datetime('now', '-N days').
    - unconfirmed_only drops rows whose target file has a later
      `skill-patch-confirmed:<file>` row in retro_findings.
    """
    params: list = [PATCH_PREFIX + "%"]
    where = ["rf.action_taken LIKE ?"]
    if window_days and window_days > 0:
        where.append("rf.created_at >= datetime('now', ?)")
        params.append(f"-{window_days} days")
    if unconfirmed_only:
        where.append(
            "NOT EXISTS ("
            "  SELECT 1 FROM retro_findings rf2"
            "  WHERE rf2.action_taken ="
            "    ? || substr(rf.action_taken, ?)"
            "    AND rf2.created_at > rf.created_at"
            ")"
        )
        params.extend([CONFIRMED_PREFIX, len(PATCH_PREFIX) + 1])

    sql = (
        "SELECT id AS finding_id, skill_run_id, task_id, action_taken, "
        "       created_at, "
        "       CAST(julianday('now') - julianday(created_at) AS INTEGER) AS age_days "
        "FROM retro_findings rf "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY created_at DESC, id DESC"
    )

    rows = []
    for row in conn.execute(sql, params):
        action = row["action_taken"]
        rows.append({
            "finding_id": row["finding_id"],
            "skill_run_id": row["skill_run_id"],
            "task_id": row["task_id"],
            "action_taken": action,
            "target_file": action[len(PATCH_PREFIX):],
            "created_at": row["created_at"],
            "age_days": row["age_days"],
        })
    return rows


def main(argv: list) -> int:
    db_path = argv[0]
    # argv[1] is config_path — reserved for future use
    parser = argparse.ArgumentParser(
        prog="tusk retro-patches",
        description=(
            "List retro_findings rows whose action_taken begins with "
            "'skill-patch:'. Use --unconfirmed to filter to patches that "
            "lack a follow-up 'skill-patch-confirmed:<file>' row — those "
            "are the patches whose effect has not yet been validated."
        ),
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=(
            "Look-back window for retro_findings.created_at. "
            f"Default {DEFAULT_WINDOW_DAYS}. 0 disables the filter."
        ),
    )
    parser.add_argument(
        "--unconfirmed",
        action="store_true",
        help=(
            "Only emit patches without a later "
            "'skill-patch-confirmed:<file>' row referencing the same file."
        ),
    )
    args = parser.parse_args(argv[2:])

    if args.window_days < 0:
        print("--window-days must be >= 0", file=sys.stderr)
        return 1

    conn = get_connection(db_path)
    try:
        payload = fetch_patches(
            conn,
            window_days=args.window_days,
            unconfirmed_only=args.unconfirmed,
        )
        print(dumps(payload))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk retro-patches [--window-days N] [--unconfirmed]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
