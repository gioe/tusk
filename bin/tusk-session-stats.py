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

import importlib.util
import logging
import os
import sqlite3
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def _load_lib():
    """Import tusk-pricing-lib.py (hyphenated filename requires importlib)."""
    lib_path = Path(__file__).resolve().parent / "tusk-pricing-lib.py"
    spec = importlib.util.spec_from_file_location("tusk_pricing_lib", lib_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lib = _load_lib()


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
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT started_at, ended_at FROM task_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()

    if not row:
        print(f"Error: No session found with id {session_id}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    started_at = lib.parse_sqlite_timestamp(row["started_at"])
    ended_at = lib.parse_sqlite_timestamp(row["ended_at"]) if row["ended_at"] else None

    # Discover transcript if not provided
    if not transcript_path:
        cwd = os.getcwd()
        project_hash = lib.derive_project_hash(cwd)
        transcript_path = lib.find_transcript(project_hash)
        if not transcript_path:
            print(
                f"Error: No JSONL transcripts found for project hash '{project_hash}'.\n"
                f"Looked in: ~/.claude/projects/{project_hash}/\n"
                "Provide the transcript path explicitly.",
                file=sys.stderr,
            )
            conn.close()
            sys.exit(1)

    if not os.path.isfile(transcript_path):
        print(f"Error: Transcript not found: {transcript_path}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    # Aggregate tokens
    totals = lib.aggregate_session(transcript_path, started_at, ended_at)

    if totals["request_count"] == 0:
        print(
            f"Warning: No assistant messages found in time window "
            f"[{started_at.isoformat()} .. {ended_at.isoformat() if ended_at else 'now'}]",
            file=sys.stderr,
        )
        conn.close()
        sys.exit(0)

    tokens_in = lib.compute_tokens_in(totals)
    tokens_out = totals["output_tokens"]
    cost = lib.compute_cost(totals)
    model = totals["model"]

    # Update DB
    conn.execute(
        """UPDATE task_sessions
           SET tokens_in = ?, tokens_out = ?, cost_dollars = ?, model = ?
           WHERE id = ?""",
        (tokens_in, tokens_out, cost, model, session_id),
    )
    conn.commit()
    conn.close()

    # Print summary
    print(f"Session {session_id} token stats updated:")
    print(f"  Model:        {model}")
    print(f"  Requests:     {totals['request_count']}")
    print(f"  Input tokens: {tokens_in:,} (base: {totals['input_tokens']:,}, "
          f"cache write 5m: {totals['cache_creation_5m_tokens']:,}, "
          f"cache write 1h: {totals['cache_creation_1h_tokens']:,}, "
          f"cache read: {totals['cache_read_input_tokens']:,})")
    print(f"  Output tokens: {tokens_out:,}")
    print(f"  Est. cost:    ${cost:.4f}")


if __name__ == "__main__":
    main()
