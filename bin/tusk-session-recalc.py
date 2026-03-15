#!/usr/bin/env python3
"""Re-run session-stats for all existing sessions to backfill corrected costs.

Iterates over task_sessions, finds the matching transcript, and recomputes
tokens/cost with the updated pricing formula.

Called by the tusk wrapper:
    tusk session-recalc
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

lib = tusk_loader.load("tusk-pricing-lib")
_db_lib = tusk_loader.load("tusk-db-lib")
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
