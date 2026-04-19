#!/usr/bin/env python3
"""Cross-retro theme rollup, grouped by category, emitted as one JSON blob.

/retro consumes this (never raw `retro_findings` rows) so that every
cross-retro pattern check is done in SQL — satisfies TASK-108 criterion 480.

Called by the tusk wrapper:
    tusk retro-themes [--window-days N] [--min-recurrence N]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path

Flags:
    --window-days N      Look-back window for `created_at` filter. Default 30.
                         0 means "all history" (no window filter).
    --min-recurrence N   Drop themes whose count is below N. Default 1 (emit
                         every theme). /retro passes 3 to surface only themes
                         appearing 3+ times in the window.

Output JSON shape (pre-aggregated tuples only — no raw row escape hatch):
    {
        "window_days": N,
        "min_recurrence": N,
        "total_findings": N,          # rows in the window; counted BEFORE
                                      # min_recurrence is applied (post-WHERE,
                                      # pre-HAVING) so callers can tell
                                      # "6 findings, only 1 recurring theme"
                                      # at a glance
        "themes": [                   # sorted by count desc, then theme asc
            {"theme": "<category>", "count": N},
            ...
        ]
    }

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
DEFAULT_MIN_RECURRENCE = 1


def fetch_themes(
    conn: sqlite3.Connection,
    *,
    window_days: int,
    min_recurrence: int,
) -> dict:
    """Aggregate retro_findings in SQL. Returns the full output payload.

    - window_days == 0 disables the date filter (all history).
    - window_days > 0 limits to rows whose created_at >= datetime('now', '-N days').
    - min_recurrence is applied via HAVING so themes below the bar never
      leave the DB; /retro never sees them.
    """
    params: list = []
    window_clause = ""
    if window_days and window_days > 0:
        window_clause = "WHERE created_at >= datetime('now', ?)"
        params.append(f"-{window_days} days")

    # Total findings in the window (not per-theme; for caller context).
    total_sql = f"SELECT COUNT(*) FROM retro_findings {window_clause}"
    total_findings = conn.execute(total_sql, params).fetchone()[0]

    # Per-theme counts; HAVING trims below the recurrence floor in SQL.
    theme_sql = f"""
        SELECT category AS theme, COUNT(*) AS count
          FROM retro_findings
         {window_clause}
         GROUP BY category
         HAVING COUNT(*) >= ?
         ORDER BY count DESC, theme ASC
    """
    rows = conn.execute(theme_sql, params + [min_recurrence]).fetchall()
    themes = [{"theme": r["theme"], "count": r["count"]} for r in rows]

    return {
        "window_days": window_days,
        "min_recurrence": min_recurrence,
        "total_findings": total_findings,
        "themes": themes,
    }


def main(argv: list) -> int:
    db_path = argv[0]
    # argv[1] is config_path — reserved for future use
    parser = argparse.ArgumentParser(
        prog="tusk retro-themes",
        description=(
            "Aggregate retro_findings by category (the 'theme') across a "
            "configurable look-back window. Output is pre-aggregated "
            "[{theme, count}] tuples — /retro never sees raw rows."
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
        "--min-recurrence",
        type=int,
        default=DEFAULT_MIN_RECURRENCE,
        help=(
            "Drop themes whose count is below this value. "
            f"Default {DEFAULT_MIN_RECURRENCE}. "
            "Use 3 to surface only recurring themes."
        ),
    )
    args = parser.parse_args(argv[2:])

    if args.window_days < 0:
        print("--window-days must be >= 0", file=sys.stderr)
        return 1
    if args.min_recurrence < 1:
        print("--min-recurrence must be >= 1", file=sys.stderr)
        return 1

    conn = get_connection(db_path)
    try:
        payload = fetch_themes(
            conn,
            window_days=args.window_days,
            min_recurrence=args.min_recurrence,
        )
        print(dumps(payload))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk retro-themes [--window-days N] [--min-recurrence N]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
