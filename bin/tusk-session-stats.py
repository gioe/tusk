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

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Per-million-token pricing (USD). Claude Code uses 1-hour prompt caching,
# so cache writes are priced at the 1h rate (2x base input).
PRICING = {
    "claude-opus-4-6": {
        "input": 5.00,
        "cache_write": 10.00,  # 1h: 2x base
        "cache_read": 0.50,    # 0.1x base
        "output": 25.00,
    },
    "claude-opus-4-5": {
        "input": 5.00,
        "cache_write": 10.00,
        "cache_read": 0.50,
        "output": 25.00,
    },
    "claude-sonnet-4-5": {
        "input": 3.00,
        "cache_write": 6.00,
        "cache_read": 0.30,
        "output": 15.00,
    },
    "claude-sonnet-4": {
        "input": 3.00,
        "cache_write": 6.00,
        "cache_read": 0.30,
        "output": 15.00,
    },
    "claude-haiku-4-5": {
        "input": 1.00,
        "cache_write": 2.00,
        "cache_read": 0.10,
        "output": 5.00,
    },
}

# Model ID aliases — Claude Code transcripts use short IDs like
# "claude-opus-4-6" but may also include dated suffixes.
MODEL_ALIASES = {
    "claude-opus-4-6-20250918": "claude-opus-4-6",
    "claude-opus-4-5-20250929": "claude-opus-4-5",
    "claude-sonnet-4-5-20250929": "claude-sonnet-4-5",
    "claude-sonnet-4-20250514": "claude-sonnet-4",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
}


def resolve_model(model_id: str) -> str:
    """Normalize a model ID to a canonical pricing key."""
    if model_id in PRICING:
        return model_id
    if model_id in MODEL_ALIASES:
        return MODEL_ALIASES[model_id]
    # Try stripping date suffix (e.g. "claude-opus-4-6-20260101")
    for key in PRICING:
        if model_id.startswith(key):
            return key
    return model_id


def find_transcript(project_dir: str) -> str | None:
    """Find the most recently modified JSONL in the Claude projects dir."""
    claude_dir = Path.home() / ".claude" / "projects" / project_dir
    if not claude_dir.is_dir():
        return None
    jsonl_files = list(claude_dir.glob("*.jsonl"))
    if not jsonl_files:
        return None
    return str(max(jsonl_files, key=lambda p: p.stat().st_mtime))


def derive_project_hash(cwd: str) -> str:
    """Derive Claude Code's project hash from a directory path.

    Claude Code uses the absolute path with '/' replaced by '-',
    e.g. /Users/foo/myproject -> -Users-foo-myproject
    """
    return cwd.replace("/", "-")


def parse_timestamp(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp, handling both Z and +00:00 suffixes."""
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts)


def parse_sqlite_timestamp(ts: str) -> datetime:
    """Parse a SQLite datetime string (UTC, no timezone info)."""
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def aggregate_session(
    transcript_path: str,
    started_at: datetime,
    ended_at: datetime | None,
) -> dict:
    """Parse a JSONL transcript and aggregate tokens within the time window.

    Returns dict with keys: input_tokens, output_tokens,
    cache_creation_input_tokens, cache_read_input_tokens, model, request_count.
    """
    seen_requests: set[str] = set()
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    model_counts: dict[str, int] = {}
    request_count = 0

    with open(transcript_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Only assistant messages have usage data
            if entry.get("type") != "assistant":
                continue

            # Check timestamp is within session window
            ts_str = entry.get("timestamp")
            if not ts_str:
                continue
            try:
                ts = parse_timestamp(ts_str)
            except (ValueError, TypeError):
                continue

            if ts < started_at:
                continue
            if ended_at and ts > ended_at:
                continue

            # Deduplicate by requestId (streaming produces multiple entries)
            request_id = entry.get("requestId")
            if not request_id:
                continue
            if request_id in seen_requests:
                continue
            seen_requests.add(request_id)
            request_count += 1

            # Extract usage
            message = entry.get("message", {})
            usage = message.get("usage", {})
            if not usage:
                continue

            totals["input_tokens"] += usage.get("input_tokens", 0)
            totals["output_tokens"] += usage.get("output_tokens", 0)
            totals["cache_creation_input_tokens"] += usage.get(
                "cache_creation_input_tokens", 0
            )
            totals["cache_read_input_tokens"] += usage.get(
                "cache_read_input_tokens", 0
            )

            # Track model usage
            model = message.get("model", "")
            if model:
                model = resolve_model(model)
                model_counts[model] = model_counts.get(model, 0) + 1

    # Determine dominant model
    dominant_model = ""
    if model_counts:
        dominant_model = max(model_counts, key=model_counts.get)

    return {
        **totals,
        "model": dominant_model,
        "model_counts": model_counts,
        "request_count": request_count,
    }


def compute_cost(totals: dict) -> float:
    """Compute cost in dollars from token totals and model."""
    model = totals.get("model", "")
    rates = PRICING.get(model)
    if not rates:
        return 0.0

    mtok = 1_000_000
    cost = (
        totals["input_tokens"] / mtok * rates["input"]
        + totals["cache_creation_input_tokens"] / mtok * rates["cache_write"]
        + totals["cache_read_input_tokens"] / mtok * rates["cache_read"]
        + totals["output_tokens"] / mtok * rates["output"]
    )
    return round(cost, 6)


def main():
    if len(sys.argv) < 4:
        print(
            "Usage: tusk session-stats <session_id> [transcript_path]",
            file=sys.stderr,
        )
        sys.exit(1)

    db_path = sys.argv[1]
    # sys.argv[2] is config_path (unused here but kept for dispatch consistency)
    session_id = sys.argv[3]

    try:
        session_id = int(session_id)
    except ValueError:
        print(f"Error: session_id must be an integer, got '{sys.argv[3]}'", file=sys.stderr)
        sys.exit(1)

    transcript_path = sys.argv[4] if len(sys.argv) > 4 else None

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

    started_at = parse_sqlite_timestamp(row["started_at"])
    ended_at = parse_sqlite_timestamp(row["ended_at"]) if row["ended_at"] else None

    # Discover transcript if not provided
    if not transcript_path:
        cwd = os.getcwd()
        project_hash = derive_project_hash(cwd)
        transcript_path = find_transcript(project_hash)
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
    totals = aggregate_session(transcript_path, started_at, ended_at)

    if totals["request_count"] == 0:
        print(
            f"Warning: No assistant messages found in time window "
            f"[{started_at.isoformat()} .. {ended_at.isoformat() if ended_at else 'now'}]",
            file=sys.stderr,
        )
        conn.close()
        sys.exit(0)

    tokens_in = (
        totals["input_tokens"]
        + totals["cache_creation_input_tokens"]
        + totals["cache_read_input_tokens"]
    )
    tokens_out = totals["output_tokens"]
    cost = compute_cost(totals)
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
          f"cache write: {totals['cache_creation_input_tokens']:,}, "
          f"cache read: {totals['cache_read_input_tokens']:,})")
    print(f"  Output tokens: {tokens_out:,}")
    print(f"  Est. cost:    ${cost:.4f}")


if __name__ == "__main__":
    main()
