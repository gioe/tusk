#!/usr/bin/env python3
"""Re-run session-stats for all existing sessions to backfill corrected costs.

Iterates over task_sessions, finds the matching transcript, and recomputes
tokens/cost with the updated pricing formula.

Called by the tusk wrapper:
    tusk session-recalc
"""

import importlib.util
import os
import sys
from pathlib import Path


def _load_lib():
    """Import tusk-pricing-lib.py (hyphenated filename requires importlib)."""
    lib_path = Path(__file__).resolve().parent / "tusk-pricing-lib.py"
    spec = importlib.util.spec_from_file_location("tusk_pricing_lib", lib_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_db_lib():
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk-db-lib.py")
    _s = importlib.util.spec_from_file_location("tusk_db_lib", _p)
    _m = importlib.util.module_from_spec(_s)
    _s.loader.exec_module(_m)
    return _m


lib = _load_lib()
_db_lib = _load_db_lib()
get_connection = _db_lib.get_connection


def main():
    if len(sys.argv) < 2:
        print("Usage: tusk session-recalc", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    # argv[2] is config_path (unused here)

    lib.load_pricing()

    transcripts = lib.find_all_transcripts_with_fallback()

    if not transcripts:
        cwd = os.getcwd()
        project_hash = lib.derive_project_hash(cwd)
        print(
            f"Error: No JSONL transcripts found.\n"
            f"Tried cwd '{cwd}', git root, and parent directories.\n"
            f"Expected transcripts under ~/.claude/projects/<hash>/ — "
            f"e.g. ~/.claude/projects/{project_hash}/",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT id, started_at, ended_at FROM task_sessions WHERE started_at IS NOT NULL"
        ).fetchall()

        if not rows:
            print("No sessions found to recalculate.")
            return

        print(f"Found {len(rows)} sessions and {len(transcripts)} transcripts")

        updated = 0
        skipped = 0

        for row in rows:
            session_id = row["id"]
            started_at = lib.parse_sqlite_timestamp(row["started_at"])
            ended_at = lib.parse_sqlite_timestamp(row["ended_at"]) if row["ended_at"] else None

            # Try each transcript to find one with matching data
            best_totals = None
            for transcript_path in transcripts:
                totals = lib.aggregate_session(transcript_path, started_at, ended_at)
                if totals["request_count"] > 0:
                    best_totals = totals
                    break

            if not best_totals or best_totals["request_count"] == 0:
                skipped += 1
                continue

            lib.update_session_stats(conn, session_id, best_totals)
            updated += 1

        conn.commit()
    finally:
        conn.close()

    print(f"Recalculated {updated} sessions, skipped {skipped} (no matching transcript)")


if __name__ == "__main__":
    main()
