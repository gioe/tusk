#!/usr/bin/env python3
"""Manage flat subscription cost rows (the ``plans`` table).

Called by the tusk wrapper:
    tusk plans set <name> <monthly_cost> [--effective-from YYYY-MM-DD] [--notes ...]
    tusk plans list [--format text|json] [--active-on YYYY-MM-DD] [--name NAME]
    tusk plans end <name> [--effective-to YYYY-MM-DD]

Records the user's declared flat-rate subscription history so cost rollups
can answer "what metered value did I consume vs what I actually paid?".
The companion time-windowed metered rollup is issue #871; the dashboard
ROI annotation is a follow-up once both halves ship. Issue #873.

EXPLICIT NON-GOAL — cross-provider usage ingestion. This file records
ONLY what the user declares. Scraping another tool's telemetry (Codex,
ChatGPT, third-party APIs) produces perpetually incomplete numbers and
erodes confidence in tusk's cost data. The honest boundary is: tusk
reports the value of work done through tusk, against the subscription
cost the user declares here.

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — subcommand + flags
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py

_db_lib = tusk_loader.load("tusk-db-lib")
get_connection = _db_lib.get_connection
load_config = _db_lib.load_config


ISO_DATE_FMT = "%Y-%m-%d"


def _today_iso() -> str:
    return date.today().strftime(ISO_DATE_FMT)


def _validate_iso_date(value: str, field: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError:
        raise SystemExit(
            f"Error: --{field} must be YYYY-MM-DD, got '{value}'"
        )
    return value


# ── Pure helper (covered by unit tests) ──────────────────────────────

def select_active_plans(plan_rows: list[dict], as_of: str) -> list[dict]:
    """Return plan rows active on the given ISO date.

    A plan is active iff ``effective_from <= as_of`` AND
    (``effective_to`` IS NULL OR ``as_of < effective_to``). The half-open
    interval matches the convention "the day a plan ends, it's already
    been replaced" — billing typically prorates per-day rather than
    double-counting the cutover day on both sides.

    This is the date-range selection helper that issue #871's eventual
    ROI line will consume. Kept pure (no DB, no I/O) so unit tests can
    exercise it directly.
    """
    return [
        p for p in plan_rows
        if p["effective_from"] <= as_of
        and (p["effective_to"] is None or as_of < p["effective_to"])
    ]


# ── Subcommands ──────────────────────────────────────────────────────

def cmd_set(args: argparse.Namespace, db_path: str, config: dict) -> int:
    effective_from = args.effective_from or _today_iso()
    _validate_iso_date(effective_from, "effective-from")
    if args.monthly_cost < 0:
        print("Error: <monthly_cost> must be non-negative", file=sys.stderr)
        return 1

    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO plans (name, monthly_cost_dollars, effective_from, notes)"
            " VALUES (?, ?, ?, ?)",
            (args.name, args.monthly_cost, effective_from, args.notes),
        )
        conn.commit()
        new_id = cur.lastrowid
    finally:
        conn.close()

    print(json.dumps({
        "id": new_id,
        "name": args.name,
        "monthly_cost_dollars": args.monthly_cost,
        "effective_from": effective_from,
        "notes": args.notes,
    }))
    return 0


def _fetch_all(conn: sqlite3.Connection, name: str | None) -> list[dict]:
    if name:
        rows = conn.execute(
            "SELECT id, name, monthly_cost_dollars, effective_from, effective_to,"
            " notes, created_at FROM plans WHERE name = ?"
            " ORDER BY effective_from, id",
            (name,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, name, monthly_cost_dollars, effective_from, effective_to,"
            " notes, created_at FROM plans"
            " ORDER BY name, effective_from, id"
        ).fetchall()
    return [dict(r) for r in rows]


def cmd_list(args: argparse.Namespace, db_path: str, config: dict) -> int:
    if args.active_on:
        _validate_iso_date(args.active_on, "active-on")

    conn = get_connection(db_path)
    try:
        rows = _fetch_all(conn, args.name)
    finally:
        conn.close()

    if args.active_on:
        rows = select_active_plans(rows, args.active_on)

    if args.format == "json":
        print(json.dumps(rows))
        return 0

    if not rows:
        if args.active_on:
            print(f"No plans active on {args.active_on}.")
        elif args.name:
            print(f"No plans recorded for '{args.name}'.")
        else:
            print("No plans recorded. Use: tusk plans set <name> <monthly_cost>")
        return 0

    print(f"{'ID':<5} {'Name':<24} {'Monthly':<10} {'From':<12} {'To':<12} {'Notes'}")
    print("-" * 90)
    for r in rows:
        to_str = r["effective_to"] or "(open)"
        notes = r["notes"] or ""
        print(
            f"{r['id']:<5} {r['name']:<24} "
            f"${r['monthly_cost_dollars']:>7.2f}  "
            f"{r['effective_from']:<12} {to_str:<12} {notes}"
        )
    print(f"\nTotal: {len(rows)}")
    return 0


def cmd_end(args: argparse.Namespace, db_path: str, config: dict) -> int:
    effective_to = args.effective_to or _today_iso()
    _validate_iso_date(effective_to, "effective-to")

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT id, effective_from FROM plans"
            " WHERE name = ? AND effective_to IS NULL"
            " ORDER BY effective_from DESC, id DESC LIMIT 1",
            (args.name,),
        ).fetchone()
        if not row:
            print(
                f"Error: no open period found for plan '{args.name}'",
                file=sys.stderr,
            )
            return 2
        if effective_to < row["effective_from"]:
            print(
                f"Error: --effective-to ({effective_to}) precedes "
                f"effective_from ({row['effective_from']}) for plan '{args.name}'",
                file=sys.stderr,
            )
            return 1
        conn.execute(
            "UPDATE plans SET effective_to = ? WHERE id = ?",
            (effective_to, row["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    print(json.dumps({
        "id": row["id"],
        "name": args.name,
        "effective_to": effective_to,
    }))
    return 0


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print("Usage: tusk plans {set|list|end} ...", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    config_path = sys.argv[2]
    config = load_config(config_path)

    parser = argparse.ArgumentParser(allow_abbrev=False,
        prog="tusk plans",
        description="Record flat subscription cost (the ROI denominator)",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    set_p = subparsers.add_parser("set", allow_abbrev=False, help="Record a new subscription period")
    set_p.add_argument("name", help="Plan name (e.g. 'claude_max_20x')")
    set_p.add_argument("monthly_cost", type=float, help="Monthly cost in USD")
    set_p.add_argument(
        "--effective-from",
        default=None,
        metavar="YYYY-MM-DD",
        help="Period start date (default: today)",
    )
    set_p.add_argument("--notes", default=None, help="Optional free-text notes")

    list_p = subparsers.add_parser("list", allow_abbrev=False, help="List recorded plans")
    list_p.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text)",
    )
    list_p.add_argument(
        "--active-on",
        default=None,
        metavar="YYYY-MM-DD",
        help="Only show plans active on this date",
    )
    list_p.add_argument("--name", default=None, help="Filter to one plan name")

    end_p = subparsers.add_parser("end", allow_abbrev=False, help="Close the open period for a plan")
    end_p.add_argument("name", help="Plan name to close")
    end_p.add_argument(
        "--effective-to",
        default=None,
        metavar="YYYY-MM-DD",
        help="Period end date (default: today)",
    )

    args = parser.parse_args(sys.argv[3:])

    if not args.command:
        parser.print_help()
        sys.exit(1)

    dispatch = {"set": cmd_set, "list": cmd_list, "end": cmd_end}
    sys.exit(dispatch[args.command](args, db_path, config))


if __name__ == "__main__":
    main()
