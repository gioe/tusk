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
import json
from pathlib import Path
import tempfile
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


HOOK_TRANSCRIPT_LIMIT = 2 * 1024 * 1024


def capture_hook(conn, context, event):
    """Replace telemetry atomically from a validated bounded Codex snapshot.

    The caller supplies a short-lived worker and connection with bounded waits.
    No task/session lifecycle fields are written, including on late delivery.
    """
    session_id = context["session_id"]
    recovery = f"Run tusk session-stats {session_id} <transcript-path> to recover telemetry."
    def deferred(reason):
        return {"systemMessage": f"Tusk telemetry deferred: {reason}. {recovery}"}
    path = Path(event["transcript_path"])
    row = conn.execute("SELECT * FROM task_sessions WHERE id=? AND task_id=?",
                       (session_id, context["task_id"])).fetchone()
    if not row:
        return deferred("session identity is no longer available")
    if row["transcript_path"] and Path(row["transcript_path"]).resolve() != path.resolve():
        return deferred("session is pinned to a different transcript")
    with path.open('rb') as stream:
        raw = stream.read(HOOK_TRANSCRIPT_LIMIT + 1)
    if len(raw) > HOOK_TRANSCRIPT_LIMIT:
        return deferred("transcript exceeds the 2 MiB hook budget")
    if not raw or not raw.endswith(b'\n'):
        return deferred("transcript is empty or still being written")
    entries = [json.loads(line) for line in raw.splitlines() if line.strip()]
    metadata = [e.get('payload', {}) for e in entries if isinstance(e, dict) and e.get('type') == 'session_meta']
    if (len(metadata) != 1 or not isinstance(metadata[0], dict)
            or metadata[0].get('id') != event['session_id']):
        return deferred("transcript session identity does not match")
    cwd = metadata[0].get('cwd')
    if (not isinstance(cwd, str) or not Path(cwd).is_absolute()
            or not Path(cwd).resolve().is_relative_to(Path(context['root']).resolve())):
        return deferred("transcript workspace does not match")
    # Reject incomplete/malformed usage instead of letting the permissive CLI
    # parser turn it into zero counts. Rate-limit-only events have no usage.
    filtered = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('payload', {}), dict):
            return deferred("transcript contains malformed events")
        payload = entry.get('payload', {})
        if entry.get('type') == 'event_msg' and payload.get('type') == 'token_count':
            info = payload.get('info')
            if info is None:
                continue
            if not isinstance(info, dict):
                return deferred("usage record is incomplete")
            usage = info.get('last_token_usage') or info.get('total_token_usage')
            if not isinstance(usage, dict) or not all(k in usage for k in ('input_tokens', 'output_tokens')):
                return deferred("usage record is incomplete")
            if any(type(v) is not int or v < 0 for v in usage.values()):
                return deferred("usage record is invalid")
            if usage.get('cached_input_tokens', 0) > usage['input_tokens']:
                return deferred("usage cache counts are invalid")
            lib.parse_timestamp(entry['timestamp'])
        filtered.append(entry)
    lib.load_pricing()
    started = lib.parse_sqlite_timestamp(row['started_at'])
    ended = lib.parse_sqlite_timestamp(row['ended_at']) if row['ended_at'] else None
    breakdown = tusk_loader.load('tusk-call-breakdown')
    with tempfile.NamedTemporaryFile(mode='w+', suffix='.jsonl', encoding='utf-8') as snapshot:
        for entry in filtered:
            snapshot.write(json.dumps(entry) + '\n')
        snapshot.flush()
        totals = lib.aggregate_session(snapshot.name, started, ended)
        if not totals['request_count']:
            return deferred("no attributable usage is available")
        unknown = [model for model in totals['model_counts'] if model not in lib.PRICING]
        if unknown:
            totals['model'] = unknown[0]
        status = lib.telemetry_status(totals)
        if status == 'model_missing':
            return deferred("model identity is missing")
        items = []
        stats = breakdown.aggregate_tool_calls([snapshot.name], started, ended, out_items=items) if status == 'captured' else None
    # Serialize competing deliveries and recheck against the latest committed
    # counters. A truncated or older snapshot must never replace richer data.
    conn.execute('BEGIN IMMEDIATE')
    try:
        current = conn.execute('SELECT * FROM task_sessions WHERE id=?', (session_id,)).fetchone()
        if (not current or current['task_id'] != context['task_id']
                or any(current[k] != row[k] for k in ('started_at', 'ended_at', 'transcript_path'))):
            conn.rollback()
            return deferred("session changed during capture")
        proposed = {'tokens_in': lib.compute_tokens_in(totals), 'tokens_out': totals['output_tokens'],
                    'request_count': totals['request_count']}
        if any(current[k] is not None and proposed[k] < current[k] for k in proposed):
            conn.rollback()
            return deferred("snapshot is older or partial; retained recorded usage")
        old_calls = conn.execute('SELECT coalesce(sum(call_count), 0) FROM tool_call_stats WHERE session_id=?',
                                 (session_id,)).fetchone()[0]
        if stats is not None and sum(v['call_count'] for v in stats.values()) < old_calls:
            conn.rollback()
            return deferred("snapshot has fewer tool calls; retained recorded usage")
        lib.update_session_stats(conn, session_id, totals)
        conn.execute("UPDATE task_sessions SET transcript_path=?, transcript_provider='codex' WHERE id=?",
                     (str(path.resolve()), session_id))
        if stats is not None:
            conn.execute('DELETE FROM tool_call_stats WHERE session_id=?', (session_id,))
            breakdown.upsert_session_stats(conn, session_id, context['task_id'], stats, commit=False)
            breakdown.insert_session_events(conn, session_id, context['task_id'], items, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if status == 'unpriced_model':
        model = ' '.join(totals['model'].split())[:100]
        return {"systemMessage": f"Tusk: no pricing for {model!r}; session {session_id} cost is unavailable. "
                f"Add pricing.json rates, then rerun tusk session-stats {session_id}. Tool costs were left unchanged."}
    return {}


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

        provider = row["transcript_provider"] or lib.active_transcript_provider()
        if not transcript_path:
            transcript_path = row["transcript_path"]
            pinned_provider = lib.transcript_provider(transcript_path)
            if pinned_provider and pinned_provider != provider:
                print(
                    f"Warning: Ignoring legacy {pinned_provider} transcript pinned for {provider} telemetry.",
                    file=sys.stderr,
                )
                transcript_path = None
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
