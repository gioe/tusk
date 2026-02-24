"""Shared transcript/pricing utilities for tusk session and criteria scripts.

Provides pricing loading, model resolution, transcript parsing, token
aggregation, and cost computation.  Imported by tusk-session-stats.py,
tusk-criteria.py, tusk-session-recalc.py, and tusk-call-breakdown.py.
"""

import json
import logging
import os
import sqlite3
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Module-level state populated by load_pricing().
PRICING: dict = {}
MODEL_ALIASES: dict = {}


def load_pricing() -> None:
    """Load model pricing and aliases from pricing.json.

    Searches next to the *calling* script first (installed layout), then
    the parent directory (source repo layout where pricing.json is at the
    repo root).  Falls back to searching next to *this* module if neither
    matches — covers the case where the caller lives in a different
    directory.
    """
    global PRICING, MODEL_ALIASES
    script_dir = Path(__file__).resolve().parent
    candidates = [
        script_dir / "pricing.json",
        script_dir.parent / "pricing.json",
    ]
    for path in candidates:
        if path.is_file():
            log.debug("Loading pricing from %s", path)
            with open(path) as f:
                data = json.load(f)
            PRICING = data.get("models", {})
            MODEL_ALIASES = data.get("aliases", {})
            return
    print(
        f"Warning: pricing.json not found (searched {', '.join(str(p) for p in candidates)}). "
        "Cost calculations will return $0.",
        file=sys.stderr,
    )


def resolve_model(model_id: str) -> str:
    """Normalize a model ID to a canonical pricing key."""
    if model_id in PRICING:
        return model_id
    if model_id in MODEL_ALIASES:
        resolved = MODEL_ALIASES[model_id]
        log.debug("Model alias: %s -> %s", model_id, resolved)
        return resolved
    # Try stripping date suffix (e.g. "claude-opus-4-6-20260101")
    for key in PRICING:
        if model_id.startswith(key):
            log.debug("Model prefix match: %s -> %s", model_id, key)
            return key
    log.debug("Unknown model (no pricing): %s", model_id)
    return model_id


def parse_timestamp(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp, handling both Z and +00:00 suffixes."""
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts)


def parse_sqlite_timestamp(ts: str) -> datetime:
    """Parse a SQLite datetime string (UTC, no timezone info).

    Handles both second-level (datetime('now')) and millisecond-level
    (strftime('%Y-%m-%d %H:%M:%f', 'now')) timestamps.
    """
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in ts else "%Y-%m-%d %H:%M:%S"
    return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)


def derive_project_hash(cwd: str) -> str:
    """Derive Claude Code's project hash from a directory path.

    Claude Code uses the absolute path with '/' replaced by '-',
    e.g. /Users/foo/myproject -> -Users-foo-myproject
    """
    return cwd.replace("/", "-")


def find_transcript(project_dir: str | None = None) -> str | None:
    """Find the most recently modified JSONL in the Claude projects dir.

    If *project_dir* is not given, derives it from the current working
    directory.
    """
    if project_dir is None:
        project_dir = derive_project_hash(os.getcwd())
    claude_dir = Path.home() / ".claude" / "projects" / project_dir
    log.debug("Looking for transcripts in %s", claude_dir)
    if not claude_dir.is_dir():
        log.debug("Directory does not exist")
        return None
    jsonl_files = list(claude_dir.glob("*.jsonl"))
    log.debug("Found %d JSONL files", len(jsonl_files))
    if not jsonl_files:
        return None
    chosen = str(max(jsonl_files, key=lambda p: p.stat().st_mtime))
    log.debug("Selected transcript: %s", chosen)
    return chosen


def aggregate_session(
    transcript_path: str,
    started_at: datetime,
    ended_at: datetime | None,
) -> dict:
    """Parse a JSONL transcript and aggregate tokens within the time window.

    Returns dict with keys: input_tokens, output_tokens,
    cache_creation_input_tokens, cache_creation_5m_tokens,
    cache_creation_1h_tokens, cache_read_input_tokens, model,
    model_counts, request_count.
    """
    log.debug("Aggregating session from %s", transcript_path)
    log.debug("Time window: %s .. %s", started_at.isoformat(),
              ended_at.isoformat() if ended_at else "now")
    seen_requests: set[str] = set()
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_creation_5m_tokens": 0,
        "cache_creation_1h_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    model_counts: dict[str, int] = {}
    request_count = 0
    lines_read = 0

    with open(transcript_path) as f:
        for line in f:
            lines_read += 1
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
            totals["cache_read_input_tokens"] += usage.get(
                "cache_read_input_tokens", 0
            )

            # Per-tier cache write tokens: prefer the nested cache_creation
            # object (ephemeral_5m_input_tokens / ephemeral_1h_input_tokens).
            # Fall back to assigning all cache_creation_input_tokens to the
            # 5m tier when the nested object is absent (older transcripts).
            cache_creation = usage.get("cache_creation")
            cache_total = usage.get("cache_creation_input_tokens", 0)
            if isinstance(cache_creation, dict):
                tokens_5m = cache_creation.get("ephemeral_5m_input_tokens", 0)
                tokens_1h = cache_creation.get("ephemeral_1h_input_tokens", 0)
                totals["cache_creation_5m_tokens"] += tokens_5m
                totals["cache_creation_1h_tokens"] += tokens_1h
            else:
                totals["cache_creation_5m_tokens"] += cache_total
            totals["cache_creation_input_tokens"] += cache_total

            # Track model usage
            model = message.get("model", "")
            if model:
                model = resolve_model(model)
                model_counts[model] = model_counts.get(model, 0) + 1

    log.debug("Lines read: %d, unique requests: %d, duplicates skipped: %d",
              lines_read, request_count, len(seen_requests) - request_count
              if len(seen_requests) > request_count else 0)
    log.debug("Token totals: %s", totals)
    log.debug("Model counts: %s", model_counts)

    # Determine dominant model
    dominant_model = ""
    if model_counts:
        dominant_model = max(model_counts, key=model_counts.get)
    log.debug("Dominant model: %s", dominant_model)

    return {
        **totals,
        "model": dominant_model,
        "model_counts": model_counts,
        "request_count": request_count,
    }


def compute_cost(totals: dict) -> float:
    """Compute cost in dollars from token totals and model.

    Uses five terms: input, cache_write_5m, cache_write_1h, cache_read, output.
    """
    model = totals.get("model", "")
    rates = PRICING.get(model)
    if not rates:
        log.debug("No pricing for model %r — cost = $0", model)
        return 0.0

    mtok = 1_000_000
    cost = (
        totals["input_tokens"] / mtok * rates["input"]
        + totals["cache_creation_5m_tokens"] / mtok * rates["cache_write_5m"]
        + totals["cache_creation_1h_tokens"] / mtok * rates["cache_write_1h"]
        + totals["cache_read_input_tokens"] / mtok * rates["cache_read"]
        + totals["output_tokens"] / mtok * rates["output"]
    )
    log.debug("Cost breakdown (model=%s): input=%d*$%.2f + cache_write_5m=%d*$%.2f "
              "+ cache_write_1h=%d*$%.2f + cache_read=%d*$%.2f + output=%d*$%.2f = $%.6f",
              model,
              totals["input_tokens"], rates["input"],
              totals["cache_creation_5m_tokens"], rates["cache_write_5m"],
              totals["cache_creation_1h_tokens"], rates["cache_write_1h"],
              totals["cache_read_input_tokens"], rates["cache_read"],
              totals["output_tokens"], rates["output"],
              cost)
    return round(cost, 6)


def compute_tokens_in(totals: dict) -> int:
    """Sum all inbound token fields into a single tokens_in value."""
    return (
        totals["input_tokens"]
        + totals["cache_creation_input_tokens"]
        + totals["cache_read_input_tokens"]
    )


def iter_tool_call_costs(
    transcript_path: str,
    started_at: datetime,
    ended_at: datetime | None,
) -> Iterator[dict]:
    """Iterate per-tool-call cost attribution within a transcript time window.

    Yields one dict per tool_use block found in assistant messages:
        tool_name             (str)      — tool identifier
        marginal_input_tokens (int)      — non-cached input tokens attributed to this call
        output_tokens         (int)      — output tokens attributed to this call
        cost                  (float)    — dollars attributed to this call
        ts                    (datetime) — tz-aware timestamp of the assistant message

    When a single assistant message contains N tool_use blocks, tokens and
    cost are split evenly across them (floor-division for token counts).
    Messages with no tool_use blocks are skipped.

    Callers must invoke load_pricing() before using this function so that
    PRICING and MODEL_ALIASES are populated.
    """
    seen_requests: set[str] = set()

    with open(transcript_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("type") != "assistant":
                continue

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

            request_id = entry.get("requestId")
            if not request_id or request_id in seen_requests:
                continue
            seen_requests.add(request_id)

            message = entry.get("message", {})
            content = message.get("content", [])
            if not isinstance(content, list):
                continue

            # Collect all tool_use block names in this message
            tools = [
                b.get("name", "(unknown)")
                for b in content
                if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
            if not tools:
                continue

            # Extract per-message usage
            usage = message.get("usage", {})
            inp = usage.get("input_tokens", 0)
            out = usage.get("output_tokens", 0)

            # Marginal cost formula: Δinput_tokens × input_price + output_tokens × output_price.
            # input_tokens here represents the non-cached tokens submitted in this round-trip
            # (i.e. the incremental tokens beyond what was already cached).
            msg_model = resolve_model(message.get("model", ""))
            rates = PRICING.get(msg_model)
            if rates:
                mtok = 1_000_000
                call_cost = (
                    inp / mtok * rates["input"]
                    + out / mtok * rates["output"]
                )
            else:
                call_cost = 0.0

            # Split evenly across N tool_use blocks in this message
            n = len(tools)
            inp_each = inp // n
            out_each = out // n
            cost_each = call_cost / n

            for tool_name in tools:
                yield {
                    "tool_name": tool_name,
                    "marginal_input_tokens": inp_each,
                    "output_tokens": out_each,
                    "cost": round(cost_each, 8),
                    "ts": ts,
                }


def upsert_criterion_tool_stats(
    conn: sqlite3.Connection,
    criterion_id: int,
    task_id: int,
    stats: dict[str, dict],
    commit: bool = True,
) -> None:
    """Write aggregated tool_call_stats rows for a criterion (upsert on UNIQUE conflict).

    Pass commit=False to defer the commit, allowing the caller to batch additional
    writes (e.g. acceptance_criteria cost columns) into a single atomic transaction.
    """
    if not stats:
        return
    for tool_name, s in stats.items():
        conn.execute(
            """INSERT INTO tool_call_stats
                   (criterion_id, task_id, tool_name, call_count, total_cost, max_cost, tokens_out, tokens_in, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(criterion_id, tool_name) DO UPDATE SET
                   call_count  = excluded.call_count,
                   total_cost  = excluded.total_cost,
                   max_cost    = excluded.max_cost,
                   tokens_out  = excluded.tokens_out,
                   tokens_in   = excluded.tokens_in,
                   computed_at = excluded.computed_at""",
            (
                criterion_id,
                task_id,
                tool_name,
                s["call_count"],
                round(s["total_cost"], 8),
                round(s["max_cost"], 8),
                s["tokens_out"],
                s.get("tokens_in", 0),
            ),
        )
    if commit:
        conn.commit()
