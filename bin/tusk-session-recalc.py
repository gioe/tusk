#!/usr/bin/env python3
"""Re-run session-stats for all existing sessions to backfill corrected costs.

Iterates over task_sessions, finds the matching transcript, and recomputes
tokens/cost with the updated pricing formula.

Called by the tusk wrapper:
    tusk session-recalc
"""

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path


def _load_lib():
    """Import tusk-pricing-lib.py (hyphenated filename requires importlib)."""
    lib_path = Path(__file__).resolve().parent / "tusk-pricing-lib.py"
    spec = importlib.util.spec_from_file_location("tusk_pricing_lib", lib_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lib = _load_lib()


def find_all_transcripts(project_hash: str) -> list[str]:
    """Find all JSONL transcript files for the project."""
    claude_dir = Path.home() / ".claude" / "projects" / project_hash
    if not claude_dir.is_dir():
        return []
    return sorted(
        [str(p) for p in claude_dir.glob("*.jsonl")],
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )


def main():
    if len(sys.argv) < 2:
        print("Usage: tusk session-recalc", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    # argv[2] is config_path (unused here)

    lib.load_pricing()

    project_hash = lib.derive_project_hash(os.getcwd())
    transcripts = find_all_transcripts(project_hash)

    if not transcripts:
        print(
            f"Error: No JSONL transcripts found for project hash '{project_hash}'.\n"
            f"Looked in: ~/.claude/projects/{project_hash}/",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, started_at, ended_at FROM task_sessions WHERE started_at IS NOT NULL"
    ).fetchall()

    if not rows:
        print("No sessions found to recalculate.")
        conn.close()
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

        tokens_in = lib.compute_tokens_in(best_totals)
        tokens_out = best_totals["output_tokens"]
        cost = lib.compute_cost(best_totals)
        model = best_totals["model"]

        conn.execute(
            """UPDATE task_sessions
               SET tokens_in = ?, tokens_out = ?, cost_dollars = ?, model = ?
               WHERE id = ?""",
            (tokens_in, tokens_out, cost, model, session_id),
        )
        updated += 1

    conn.commit()
    conn.close()

    print(f"Recalculated {updated} sessions, skipped {skipped} (no matching transcript)")


if __name__ == "__main__":
    main()
