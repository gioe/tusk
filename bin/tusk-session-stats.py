#!/usr/bin/env python3
"""Token and cost tracking for tusk task sessions.

Parses Claude Code JSONL transcripts, aggregates token usage per session,
and updates the task_sessions table with tokens_in, tokens_out, cost_dollars,
and model.

Called by the tusk wrapper:
    tusk session-stats <session_id> [transcript_path]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — session_id + optional transcript path
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

log = logging.getLogger(__name__)

lib = tusk_loader.load("tusk-pricing-lib")
_db_lib = tusk_loader.load("tusk-db-lib")
get_connection = _db_lib.get_connection


def _format_duration(seconds) -> str:
    """Human-readable duration; mirrors tusk-task-summary's formatter."""
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    mins, secs = divmod(seconds, 60)
    if mins < 60:
        return f"{mins}m {secs}s" if secs else f"{mins}m"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m" if mins else f"{hours}h"


def main():
    # Extract --debug before manual positional parsing
    argv = sys.argv[1:]
    debug = "--debug" in argv
    if debug:
        argv = [a for a in argv if a != "--debug"]

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="[debug] %(message)s",
        stream=sys.stderr,
    )

    lib.load_pricing()

    if len(argv) < 3:
        print(
            "Usage: tusk session-stats [--debug] <session_id> [transcript_path]",
            file=sys.stderr,
        )
        sys.exit(1)

    db_path = argv[0]
    # argv[1] is config_path (unused here but kept for dispatch consistency)
    session_id = argv[2]

    try:
        session_id = int(session_id)
    except ValueError:
        print(f"Error: session_id must be an integer, got '{argv[2]}'", file=sys.stderr)
        sys.exit(1)

    transcript_path = argv[3] if len(argv) > 3 else None
    log.debug("DB path: %s, session_id: %d, transcript_path: %s",
              db_path, session_id, transcript_path)

    # Read session timestamps from DB
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT started_at, ended_at, transcript_path, transcript_provider "
            "FROM task_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()

        if not row:
            print(f"Error: No session found with id {session_id}", file=sys.stderr)
            sys.exit(1)

        started_at = lib.parse_sqlite_timestamp(row["started_at"])
        ended_at = lib.parse_sqlite_timestamp(row["ended_at"]) if row["ended_at"] else None

        transcript_path = transcript_path or row["transcript_path"]
        provider = row["transcript_provider"] or lib.active_transcript_provider()
        if not transcript_path:
            transcript_path = lib.find_transcript(provider=provider)
            if not transcript_path:
                print(
                    f"Warning: No {provider} transcript found; telemetry is unavailable.",
                    file=sys.stderr,
                )
                conn.execute(
                    "UPDATE task_sessions SET model = ?, telemetry_status = ? WHERE id = ?",
                    (f"({provider} transcript missing)", "transcript_missing", session_id),
                )
                conn.commit()
                return

        if not os.path.isfile(transcript_path):
            print(f"Error: Transcript not found: {transcript_path}", file=sys.stderr)
            conn.execute(
                "UPDATE task_sessions SET model = ?, telemetry_status = ? WHERE id = ?",
                (f"({provider} transcript missing)", "transcript_missing", session_id),
            )
            conn.commit()
            return

        conn.execute(
            "UPDATE task_sessions SET transcript_path = ?, transcript_provider = ? WHERE id = ?",
            (transcript_path, provider, session_id),
        )

        # Aggregate tokens
        totals = lib.aggregate_session(transcript_path, started_at, ended_at)

        if totals["request_count"] == 0:
            print(
                f"Warning: No assistant messages found in time window "
                f"[{started_at.isoformat()} .. {ended_at.isoformat() if ended_at else 'now'}]",
                file=sys.stderr,
            )
            conn.execute(
                "UPDATE task_sessions SET model = '(no attributable usage)', "
                "telemetry_status = 'no_usage' WHERE id = ?",
                (session_id,),
            )
            conn.commit()
            return

        tokens_in = lib.compute_tokens_in(totals)
        tokens_out = totals["output_tokens"]
        cost = lib.optional_cost(totals)
        model = totals["model"]

        # Update DB
        lib.update_session_stats(conn, session_id, totals)
        conn.commit()

        # Active time = idle-gap-discounted active_seconds (issue #1069),
        # falling back to wall (ended - started) for the rare case where the
        # transcript yielded no usable timestamps — the same COALESCE(
        # active_seconds, duration_seconds) fallback task-summary applies so
        # legacy/empty rows render a sensible value rather than "—" (issue #1086).
        active_seconds = totals.get("active_seconds")
        if active_seconds is None and ended_at is not None:
            active_seconds = max(0, int((ended_at - started_at).total_seconds()))

        # Print summary
        print(f"Session {session_id} token stats updated:")
        print(f"  Model:        {model}")
        print(f"  Requests:     {totals['request_count']}")
        print(f"  Input tokens: {tokens_in:,} (base: {totals['input_tokens']:,}, "
              f"cache write 5m: {totals['cache_creation_5m_tokens']:,}, "
              f"cache write 1h: {totals['cache_creation_1h_tokens']:,}, "
              f"cache read: {totals['cache_read_input_tokens']:,})")
        print(f"  Output tokens: {tokens_out:,}")
        cost_text = f"${cost:.4f}" if cost is not None else "unavailable (unpriced model)"
        print(f"  Est. cost:    {cost_text}")
        print(f"  Active time:  {_format_duration(active_seconds)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
