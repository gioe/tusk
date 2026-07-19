"""Shared transcript/pricing utilities for tusk session and criteria scripts.

Provides pricing loading, model resolution, transcript parsing, token
aggregation, and cost computation.  Imported by tusk-session-stats.py,
tusk-criteria.py, tusk-session-recalc.py, and tusk-call-breakdown.py.

Both this file and tusk-db-lib.py are loaded at runtime via tusk_loader:

    lib = tusk_loader.load("tusk-pricing-lib")
    _db_lib = tusk_loader.load("tusk-db-lib")
"""

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Module-level state populated by load_pricing().
PRICING: dict = {}
MODEL_ALIASES: dict = {}

# Context window sizes (in tokens) for known models.
#
# This table is a *fallback* only — the authoritative source is the
# per-model ``context_window`` field in pricing.json, read by
# get_context_window() once load_pricing() has populated PRICING. The
# literals below cover callers that resolve a window before (or without)
# ever calling load_pricing(); keep their values in sync with pricing.json.
# Registering a *new* model only requires a pricing.json entry — no edit here.
CONTEXT_WINDOW: dict[str, int] = {
    "claude-fable-5": 1_000_000,
    "claude-mythos-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-5": 200_000,
    "claude-opus-4-1": 200_000,
    "claude-opus-4": 200_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-haiku-3-5": 200_000,
    "claude-haiku-3": 200_000,
}
CONTEXT_WINDOW_DEFAULT = 200_000


def get_context_window(model: str) -> int:
    """Return the context window size (in tokens) for *model*.

    Resolution order:
      1. The model's ``context_window`` field in loaded pricing.json data
         (PRICING, populated by load_pricing()) — the single source of truth,
         so a new model is registered in pricing.json alone.
      2. The hardcoded CONTEXT_WINDOW fallback table, for callers that resolve
         a window before (or without) ever calling load_pricing().
      3. CONTEXT_WINDOW_DEFAULT, only when the model is absent from both.
    """
    entry = PRICING.get(model)
    if isinstance(entry, dict):
        window = entry.get("context_window")
        if isinstance(window, int):
            return window
    return CONTEXT_WINDOW.get(model, CONTEXT_WINDOW_DEFAULT)


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


def _compact(text: str) -> str:
    """Collapse interior whitespace so transcript-derived text fits one line."""
    return " ".join((text or "").split())


def derive_project_hash(cwd: str) -> str:
    """Derive Claude Code's project hash from a directory path.

    Claude Code uses the absolute path with '/' replaced by '-',
    e.g. /Users/foo/myproject -> -Users-foo-myproject
    """
    return cwd.replace("/", "-")


def _jsonl_files_for_hash(project_hash: str) -> list[Path]:
    """Return JSONL files for a given project hash, or [] if none found."""
    claude_dir = Path.home() / ".claude" / "projects" / project_hash
    log.debug("Looking for transcripts in %s", claude_dir)
    if not claude_dir.is_dir():
        log.debug("Directory does not exist: %s", claude_dir)
        return []
    files = list(claude_dir.glob("*.jsonl"))
    log.debug("Found %d JSONL files in %s", len(files), claude_dir)
    return files


def _git_context_dirs(start: str) -> list[str]:
    """Return the checkout root and primary checkout relevant to *start*."""
    seen: set[str] = set()
    contexts: list[str] = []

    def add(path: str) -> None:
        if path not in seen:
            seen.add(path)
            contexts.append(path)

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True, encoding="utf-8",
            cwd=start,
            timeout=5,
        )
        if result.returncode == 0:
            add(result.stdout.strip())
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True, encoding="utf-8",
            cwd=start,
            timeout=5,
        )
        if result.returncode == 0:
            common_dir_text = result.stdout.strip()
            if common_dir_text:
                common_dir = Path(common_dir_text)
                if not common_dir.is_absolute():
                    common_dir = Path(start) / common_dir
                if common_dir.name == ".git":
                    add(str(common_dir.parent))
    except Exception:
        pass

    return contexts


def _candidate_dirs(start: str) -> list[str]:
    """Return candidate directories to try for transcript discovery.

    Order: cwd, git root (if different), the primary checkout that owns a git
    worktree's common dir, then each parent up to filesystem root. Deduplicates
    while preserving order.
    """
    seen: set[str] = set()
    candidates: list[str] = []

    def add(path: str) -> None:
        if path not in seen:
            seen.add(path)
            candidates.append(path)

    add(start)
    for context in _git_context_dirs(start):
        add(context)

    # Walk up parent directories
    p = Path(start).parent
    while str(p) != str(p.parent):
        add(str(p))
        p = p.parent

    return candidates


def _transcript_cwd(jsonl: Path) -> str | None:
    """Read the first usable top-level cwd from a transcript."""
    try:
        with jsonl.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle):
                if line_number >= 200:
                    break
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                cwd = entry.get("cwd") if isinstance(entry, dict) else None
                if isinstance(cwd, str) and cwd:
                    return os.path.realpath(cwd)
    except OSError:
        return None
    return None


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _validated_descendant_files(repo_root: str) -> list[Path]:
    """Return prefix-matched transcripts whose recorded cwd belongs to root."""
    projects_dir = Path.home() / ".claude" / "projects"
    root_real = os.path.realpath(repo_root)
    prefixes = {
        derive_project_hash(repo_root) + "-",
        derive_project_hash(root_real) + "-",
    }
    files: list[Path] = []
    if not projects_dir.is_dir():
        return files

    for prefix in prefixes:
        for transcript_dir in projects_dir.glob(prefix + "*"):
            if not transcript_dir.is_dir():
                continue
            for jsonl in transcript_dir.glob("*.jsonl"):
                cwd = _transcript_cwd(jsonl)
                if cwd is not None and _is_within(cwd, root_real):
                    files.append(jsonl)
    return files


def _dedupe_jsonls(files: list[Path]) -> list[Path]:
    unique: dict[str, Path] = {}
    for jsonl in files:
        unique.setdefault(os.path.realpath(str(jsonl)), jsonl)
    return list(unique.values())


def _relevant_transcripts(start: str) -> list[Path]:
    """Return transcripts for the launch path, repo roots, and subdirectories."""
    primary_dirs = [start, *_git_context_dirs(start)]
    files: list[Path] = []
    seen_dirs: set[str] = set()
    for candidate in primary_dirs:
        candidate_real = os.path.realpath(candidate)
        if candidate_real in seen_dirs:
            continue
        seen_dirs.add(candidate_real)
        # Preserve the exact-path hash Claude used at launch; realpath is only
        # for containment checks and deduplication.
        files.extend(_jsonl_files_for_hash(derive_project_hash(candidate)))
        files.extend(_validated_descendant_files(candidate))

    files = _dedupe_jsonls(files)
    if files:
        return files

    # Preserve the legacy broad-parent fallback only when the project-scoped
    # search found nothing. Parent hashes can represent unrelated sessions and
    # must not compete by mtime with an actual repo transcript.
    for candidate in _candidate_dirs(start):
        candidate_real = os.path.realpath(candidate)
        if candidate_real in seen_dirs:
            continue
        fallback = _jsonl_files_for_hash(derive_project_hash(candidate))
        if fallback:
            return _dedupe_jsonls(fallback)
    return []


def _newest_transcript(files: list[Path]) -> str | None:
    existing: list[Path] = []
    for jsonl in files:
        try:
            jsonl.stat()
        except OSError:
            continue
        existing.append(jsonl)
    if existing:
        chosen = str(max(existing, key=lambda p: p.stat().st_mtime))
        log.debug("Selected transcript: %s", chosen)
        return chosen

    log.debug("No JSONL transcripts found after trying all candidate directories")
    return None


_CODEX_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9-]+$")


def active_transcript_provider() -> str:
    """Return the provider identified by the current agent runtime."""
    return "codex" if os.environ.get("CODEX_THREAD_ID") else "claude"


def transcript_provider(transcript_path: str | None) -> str | None:
    """Infer a provider from a known transcript path."""
    if not transcript_path:
        return None
    normalized = str(Path(transcript_path)).replace("\\", "/")
    if "/.codex/sessions/" in normalized:
        return "codex"
    if "/.claude/projects/" in normalized:
        return "claude"
    return None


def _find_codex_transcript(thread_id: str | None = None) -> str | None:
    """Resolve one Codex rollout by its runtime thread identity."""
    thread_id = thread_id or os.environ.get("CODEX_THREAD_ID", "")
    if not thread_id or not _CODEX_THREAD_ID_RE.fullmatch(thread_id):
        return None
    codex_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    sessions = codex_root / "sessions"
    matches = list(sessions.glob(f"*/*/*/*-{thread_id}.jsonl"))
    return _newest_transcript(matches)


def find_transcript(
    project_dir: str | None = None,
    *,
    provider: str | None = None,
) -> str | None:
    """Find the transcript for the active provider runtime.

    A known Codex thread is resolved by exact rollout identity and never falls
    back to Claude's mtime-based project search. Without a Codex identity,
    existing Claude discovery remains unchanged.

    If *project_dir* is not given, tries multiple candidate directories:
    1. os.getcwd()
    2. git root (via git rev-parse --show-toplevel)
    3. primary checkout root for task worktrees (via git common dir)
    4. Each parent directory walking up to the filesystem root

    Returns the most recently modified JSONL found, or None if nothing found.
    """
    provider = provider or ("claude" if project_dir is not None else active_transcript_provider())
    if provider == "codex":
        return _find_codex_transcript()
    if provider != "claude":
        return None

    if project_dir is not None:
        if os.path.isdir(project_dir):
            return _newest_transcript(_relevant_transcripts(project_dir))
        # Caller supplied an explicit hash — use it directly (legacy behaviour).
        files = _jsonl_files_for_hash(project_dir)
        if not files:
            return None
        return str(max(files, key=lambda p: p.stat().st_mtime))

    return _newest_transcript(_relevant_transcripts(os.getcwd()))


def find_all_transcripts_with_fallback(
    start_dir: str | None = None,
    *,
    provider: str | None = None,
) -> list[str]:
    """Find all JSONL transcripts, trying multiple candidate directories.

    Returns every relevant CWD/root/worktree/subdirectory transcript, deduped
    and sorted by mtime descending. Broad parent hashes remain a last-resort
    fallback when no project-scoped transcript exists.
    """
    provider = provider or ("claude" if start_dir is not None else active_transcript_provider())
    if provider == "codex":
        path = _find_codex_transcript()
        return [path] if path else []
    if provider != "claude":
        return []
    if start_dir is None:
        start_dir = os.getcwd()

    files = _relevant_transcripts(start_dir)
    existing = [p for p in files if p.is_file()]
    return sorted(
        [str(p) for p in existing],
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )


def _user_prompt_text(message: dict) -> str:
    """Extract real user-typed text from a Claude Code transcript user entry.

    Returns the concatenated text of all non-tool_result blocks. A block is
    counted when content is a plain string, or when it's a list of dicts
    where the block's ``type`` is anything other than ``tool_result`` (i.e.
    plain ``text`` blocks the user actually typed). Synthetic tool_result
    payloads — which can be arbitrarily large but reflect what the model
    saw, not what the user wrote — are excluded.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                continue
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    return ""


def estimate_tokens_from_chars(chars: int) -> int:
    """Approximate token count from character length (chars / 4).

    Anthropic does not expose a per-message tokenizer, and assistant
    ``input_tokens`` aggregates the full request (cache + new content), so
    the user prompt has to be estimated. Four chars/token is the standard
    rough approximation across Claude tokenizers; the absolute number is
    inexact, but the trend stays meaningful.
    """
    if chars <= 0:
        return 0
    return chars // 4


# Idle-gap threshold for active-duration computation (issue #1069). Gaps
# between consecutive transcript events above this many seconds count as
# idle (an overnight pause, a stepped-away operator) and contribute nothing
# to active_seconds; gaps at or below it count in full.
IDLE_GAP_THRESHOLD_SECONDS = 600


def compute_active_seconds(
    timestamps: list,
    threshold: int = IDLE_GAP_THRESHOLD_SECONDS,
) -> int:
    """Sum consecutive-event deltas at or below *threshold* seconds.

    The transcript's per-event timestamps (user prompts, assistant messages,
    token_count events) approximate when work actually happened; idle gaps
    above the threshold are discounted entirely so a session left open
    overnight reports active time near the real working time (issue #1069).
    Fewer than two events yields 0.
    """
    if len(timestamps) < 2:
        return 0
    ordered = sorted(timestamps)
    active = 0.0
    for prev, curr in zip(ordered, ordered[1:]):
        delta = (curr - prev).total_seconds()
        if 0 < delta <= threshold:
            active += delta
    return int(active)


def _codex_model_from_entry(entry: dict) -> str:
    """Read the active model from current Codex rollout event shapes."""
    entry_type = entry.get("type")
    payload = entry.get("payload", {})
    model = ""
    if entry_type == "turn_context":
        model = payload.get("model", "")
    elif entry_type == "event_msg" and payload.get("type") == "thread_settings_applied":
        model = payload.get("thread_settings", {}).get("model", "")
    return resolve_model(model) if isinstance(model, str) and model else ""


def _codex_turn_usage(info: dict, previous_total: dict | None) -> tuple[dict, dict | None]:
    """Return per-turn Codex usage, deriving deltas when only totals exist."""
    total = info.get("total_token_usage")
    last = info.get("last_token_usage")
    if isinstance(last, dict) and last:
        return last, total if isinstance(total, dict) else previous_total
    if not isinstance(total, dict):
        return {}, previous_total
    fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    if previous_total is None:
        usage = {field: max(0, total.get(field, 0) or 0) for field in fields}
    else:
        usage = {
            field: max(0, (total.get(field, 0) or 0) - (previous_total.get(field, 0) or 0))
            for field in fields
        }
    return usage, total


def aggregate_session(
    transcript_path: str,
    started_at: datetime,
    ended_at: datetime | None,
    *,
    stop_at_idle_gap: bool = False,
) -> dict:
    """Parse a JSONL transcript and aggregate tokens within the time window.

    Returns dict with keys: input_tokens, output_tokens,
    cache_creation_input_tokens, cache_creation_5m_tokens,
    cache_creation_1h_tokens, cache_read_input_tokens, model,
    model_counts, request_count, user_prompt_tokens, user_prompt_count,
    active_seconds (idle-gap-discounted, issue #1069).
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
    peak_context_tokens = 0
    first_context_tokens: int | None = None
    last_context_tokens: int | None = None
    context_window: int | None = None
    codex_meta: dict | None = None
    current_codex_model = ""
    previous_codex_total: dict | None = None
    user_prompt_tokens = 0
    user_prompt_count = 0
    event_timestamps: list = []
    previous_event_ts = None

    def record_event_timestamp(ts: datetime) -> bool:
        nonlocal previous_event_ts
        if stop_at_idle_gap and previous_event_ts is not None:
            delta = (ts - previous_event_ts).total_seconds()
            if delta > IDLE_GAP_THRESHOLD_SECONDS:
                return False
        event_timestamps.append(ts)
        previous_event_ts = ts
        return True

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

            event_model = _codex_model_from_entry(entry)
            if event_model:
                current_codex_model = event_model

            if entry.get("type") == "event_msg" and entry.get("payload", {}).get("type") == "token_count":
                info = entry.get("payload", {}).get("info", {})
                usage, previous_codex_total = _codex_turn_usage(info, previous_codex_total)
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
                if not record_event_timestamp(ts):
                    break

                raw_input = usage.get("input_tokens", 0)
                turn_cache_read = usage.get("cached_input_tokens", 0)
                turn_input = max(0, raw_input - turn_cache_read)
                # Codex output_tokens already includes the reasoning subset.
                turn_output = usage.get("output_tokens", 0)
                turn_context = raw_input

                totals["input_tokens"] += turn_input
                totals["output_tokens"] += turn_output
                totals["cache_read_input_tokens"] += turn_cache_read
                request_count += 1
                if current_codex_model:
                    model_counts[current_codex_model] = model_counts.get(current_codex_model, 0) + 1

                if turn_context > peak_context_tokens:
                    peak_context_tokens = turn_context
                if first_context_tokens is None:
                    first_context_tokens = turn_context
                last_context_tokens = turn_context

                model_context_window = info.get("model_context_window")
                if isinstance(model_context_window, int):
                    context_window = model_context_window
                continue

            if entry.get("type") == "user" and not entry.get("isMeta"):
                ts_str = entry.get("timestamp")
                if ts_str:
                    try:
                        ts = parse_timestamp(ts_str)
                    except (ValueError, TypeError):
                        ts = None
                    if ts is not None:
                        if ts < started_at:
                            continue
                        if ended_at and ts > ended_at:
                            continue
                        if not record_event_timestamp(ts):
                            break
                        text = _user_prompt_text(entry.get("message", {}))
                        chars = len(text)
                        if chars > 0:
                            user_prompt_count += 1
                            user_prompt_tokens += estimate_tokens_from_chars(chars)
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
            if not record_event_timestamp(ts):
                break

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

            turn_input = usage.get("input_tokens", 0)
            turn_cache_read = usage.get("cache_read_input_tokens", 0)
            turn_cache_write = usage.get("cache_creation_input_tokens", 0)
            turn_context = turn_input + turn_cache_read + turn_cache_write

            totals["input_tokens"] += turn_input
            totals["output_tokens"] += usage.get("output_tokens", 0)
            totals["cache_read_input_tokens"] += turn_cache_read

            if turn_context > peak_context_tokens:
                peak_context_tokens = turn_context
            if first_context_tokens is None:
                first_context_tokens = turn_context
            last_context_tokens = turn_context

            # Per-tier cache write tokens: prefer the nested cache_creation
            # object (ephemeral_5m_input_tokens / ephemeral_1h_input_tokens).
            # Fall back to assigning all cache_creation_input_tokens to the
            # 5m tier when the nested object is absent (older transcripts).
            cache_creation = usage.get("cache_creation")
            if isinstance(cache_creation, dict):
                tokens_5m = cache_creation.get("ephemeral_5m_input_tokens", 0)
                tokens_1h = cache_creation.get("ephemeral_1h_input_tokens", 0)
                totals["cache_creation_5m_tokens"] += tokens_5m
                totals["cache_creation_1h_tokens"] += tokens_1h
            else:
                totals["cache_creation_5m_tokens"] += turn_cache_write
            totals["cache_creation_input_tokens"] += turn_cache_write

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
    elif request_count:
        codex_meta = _lookup_codex_thread_meta(transcript_path)
        dominant_model = resolve_model(codex_meta.get("model", ""))
    log.debug("Dominant model: %s", dominant_model)

    return {
        **totals,
        "model": dominant_model,
        "model_counts": model_counts,
        "request_count": request_count,
        "peak_context_tokens": peak_context_tokens,
        "first_context_tokens": first_context_tokens,
        "last_context_tokens": last_context_tokens,
        "context_window": context_window or get_context_window(dominant_model),
        "user_prompt_tokens": user_prompt_tokens,
        "user_prompt_count": user_prompt_count,
        "active_seconds": compute_active_seconds(event_timestamps),
    }


# Models already warned about this process — one stderr line per model, not per call.
_WARNED_UNPRICED_MODELS: set = set()


def _warn_unpriced_model(model: str) -> None:
    if model in _WARNED_UNPRICED_MODELS:
        return
    _WARNED_UNPRICED_MODELS.add(model)
    print(
        f"Warning: unknown model {model!r} — no pricing.json entry; cost recorded as $0. "
        "Add the model to pricing.json so cost analytics stop silently zeroing.",
        file=sys.stderr,
    )


def compute_cost(totals: dict) -> float:
    """Compute cost in dollars from token totals and model.

    Uses five terms: input, cache_write_5m, cache_write_1h, cache_read, output.
    """
    model = totals.get("model", "")
    rates = PRICING.get(model)
    if not rates:
        tokens = sum(
            totals.get(field, 0) or 0
            for field in (
                "input_tokens",
                "cache_creation_5m_tokens",
                "cache_creation_1h_tokens",
                "cache_read_input_tokens",
                "output_tokens",
            )
        )
        if tokens > 0:
            _warn_unpriced_model(model)
        else:
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


def telemetry_status(totals: dict) -> str:
    """Classify parsed telemetry without replacing unavailable data with zero."""
    if not totals.get("request_count"):
        return "no_usage"
    model = totals.get("model", "")
    if not model:
        return "model_missing"
    if model not in PRICING:
        return "unpriced_model"
    return "captured"


def optional_cost(totals: dict) -> float | None:
    """Return an estimate only when the parsed model has configured pricing."""
    return compute_cost(totals) if telemetry_status(totals) == "captured" else None


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
    pending_codex_calls: list[tuple[str, str]] = []
    codex_meta = _lookup_codex_thread_meta(transcript_path)
    codex_model = resolve_model(codex_meta.get("model", ""))
    previous_codex_total: dict | None = None

    with open(transcript_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_model = _codex_model_from_entry(entry)
            if event_model:
                codex_model = event_model

            if entry.get("type") == "response_item":
                payload = entry.get("payload", {})
                call_id = payload.get("call_id")
                payload_type = payload.get("type")
                if payload_type == "function_call" and call_id:
                    pending_codex_calls.append((call_id, payload.get("name", "(unknown)")))
                elif payload_type == "web_search_call" and call_id:
                    pending_codex_calls.append((call_id, "web_search"))
                continue

            if entry.get("type") == "event_msg" and entry.get("payload", {}).get("type") == "token_count":
                info = entry.get("payload", {}).get("info", {})
                usage, previous_codex_total = _codex_turn_usage(info, previous_codex_total)
                ts_str = entry.get("timestamp")
                if not ts_str:
                    continue
                try:
                    ts = parse_timestamp(ts_str)
                except (ValueError, TypeError):
                    continue
                if ts < started_at:
                    pending_codex_calls.clear()
                    continue
                if ended_at and ts > ended_at:
                    break
                if not pending_codex_calls:
                    continue

                raw_input = usage.get("input_tokens", 0)
                cache_read = usage.get("cached_input_tokens", 0)
                inp = max(0, raw_input - cache_read)
                out = usage.get("output_tokens", 0)
                rates = PRICING.get(codex_model)
                if rates:
                    mtok = 1_000_000
                    call_cost = (
                        inp / mtok * rates["input"]
                        + cache_read / mtok * rates["cache_read"]
                        + out / mtok * rates["output"]
                    )
                else:
                    call_cost = 0.0

                n = len(pending_codex_calls)
                inp_each = inp // n
                out_each = out // n
                cost_each = call_cost / n

                for _, tool_name in pending_codex_calls:
                    yield {
                        "tool_name": tool_name,
                        "marginal_input_tokens": inp_each,
                        "output_tokens": out_each,
                        "cost": round(cost_each, 8),
                        "ts": ts,
                    }
                pending_codex_calls.clear()
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


def iter_tool_errors(
    transcript_path: str,
    started_at: datetime,
    ended_at: datetime | None,
) -> Iterator[dict]:
    """Iterate tool failures within a transcript time window.

    Yields one dict per user-typed `tool_result` block whose `is_error` flag is
    truthy — this covers every failure the Claude Code framework records, i.e.
    non-zero Bash exits, Edit/Read/Write guard errors, sub-agent errors, and
    user-rejected ExitPlanMode calls. The timestamp filter uses the user
    message's own `timestamp`, not the originating assistant call's — same
    convention as `iter_tool_call_costs`.

    Output shape:
        tool_name (str)       — tool identifier (resolved from the assistant
                                 message that invoked the tool; '(unknown)' if
                                 the invocation is missing or split across a
                                 transcript split)
        error_text (str)      — raw error payload (trimmed; the `<tool_use_error>`
                                 wrapper stripped when present)
        ts (datetime)         — tz-aware timestamp of the user message

    Because assistant `tool_use` blocks always precede their matching user
    `tool_result` blocks in JSONL order, a single pass suffices: we maintain
    a running `tool_use_id -> tool_name` map and emit errors as we see them.
    """
    tool_names: dict[str, str] = {}

    with open(transcript_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("type") == "response_item":
                payload = entry.get("payload", {})
                call_id = payload.get("call_id")
                payload_type = payload.get("type")
                if payload_type == "function_call" and call_id:
                    tool_names[call_id] = payload.get("name", "(unknown)")
                elif payload_type == "web_search_call" and call_id:
                    tool_names[call_id] = "web_search"
                continue

            if entry.get("type") == "event_msg":
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

                payload = entry.get("payload", {})
                payload_type = payload.get("type", "")
                if not payload_type.endswith("_end"):
                    continue
                if payload.get("status") != "failed":
                    continue
                call_id = payload.get("call_id")
                tool_name = tool_names.get(call_id, "(unknown)")
                error_text = _compact(payload.get("stderr", ""))
                if not error_text and payload.get("exit_code") is not None:
                    error_text = f"Exit code {payload.get('exit_code')}"
                yield {
                    "tool_name": tool_name,
                    "error_text": error_text,
                    "ts": ts,
                }
                continue

            entry_type = entry.get("type")
            message = entry.get("message", {}) if isinstance(entry.get("message"), dict) else {}
            content = message.get("content")
            if not isinstance(content, list):
                continue

            if entry_type == "assistant":
                # Learn the tool_name for every tool_use in this message so we
                # can attribute any later error back to the right tool.
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_use_id = block.get("id")
                        tool_name = block.get("name")
                        if tool_use_id and tool_name:
                            tool_names[tool_use_id] = tool_name
                continue

            if entry_type != "user":
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

            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_result":
                    continue
                if not block.get("is_error"):
                    continue
                tool_name = tool_names.get(block.get("tool_use_id"), "(unknown)")
                yield {
                    "tool_name": tool_name,
                    "error_text": _extract_error_text(block.get("content")),
                    "ts": ts,
                }


def _extract_error_text(raw) -> str:
    """Normalize a `tool_result` error payload into a single compact string.

    The `content` field on a tool_result block is either a string or a list of
    `{"type": "text", "text": "..."}` dicts depending on the tool. We join the
    text segments, strip the `<tool_use_error>...</tool_use_error>` wrapper
    when present (Claude Code emits it for framework-level rejections), and
    collapse interior whitespace so the result renders cleanly in a single
    table cell.
    """
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, list):
        parts: list[str] = []
        for c in raw:
            if isinstance(c, dict) and c.get("type") == "text":
                t = c.get("text")
                if isinstance(t, str):
                    parts.append(t)
        text = "\n".join(parts)
    else:
        text = ""

    text = text.strip()
    if text.startswith("<tool_use_error>") and text.endswith("</tool_use_error>"):
        text = text[len("<tool_use_error>"): -len("</tool_use_error>")].strip()
    return _compact(text)


def _lookup_codex_thread_meta(transcript_path: str) -> dict:
    """Return Codex metadata including the latest event-carried model."""
    result: dict = {}
    try:
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "session_meta":
                    payload = entry.get("payload", {})
                    if isinstance(payload, dict):
                        result.update(payload)
                model = _codex_model_from_entry(entry)
                if model:
                    result["model"] = model
    except OSError:
        pass
    return result


def update_session_stats(conn: sqlite3.Connection, session_id: int, totals: dict) -> None:
    """Write aggregated token/cost stats for a session to task_sessions.

    Computes tokens_in, tokens_out, cost_dollars, model, request_count, and
    context token fields from *totals* (as returned by aggregate_session())
    and executes the UPDATE. Does not commit — the caller is responsible
    for committing.
    """
    tokens_in = compute_tokens_in(totals)
    tokens_out = totals["output_tokens"]
    cost = optional_cost(totals)
    status = telemetry_status(totals)
    model = totals["model"]
    peak_context = totals.get("peak_context_tokens")
    first_context = totals.get("first_context_tokens")
    last_context = totals.get("last_context_tokens")
    request_count = totals.get("request_count")
    context_window = get_context_window(model) if model else None
    # Cache split — schema 74 (issue #872). The Anthropic usage block
    # already exposes these separately (cache read priced ~10%, cache write
    # ~125%); aggregate_session() collects them into the three totals keys
    # below. Persisting them is what makes cache-hit-rate visible in stored
    # data and provider-agnostic token repricing possible.
    cache_read_tokens_in = totals.get("cache_read_input_tokens", 0)
    cache_write_tokens_in = totals.get("cache_creation_input_tokens", 0)
    uncached_tokens_in = totals.get("input_tokens", 0)

    conn.execute(
        """UPDATE task_sessions
           SET tokens_in = ?, tokens_out = ?, cost_dollars = ?, model = ?,
               peak_context_tokens = ?, first_context_tokens = ?, last_context_tokens = ?,
               context_window = ?, request_count = ?,
               cache_read_tokens_in = ?, cache_write_tokens_in = ?, uncached_tokens_in = ?
           WHERE id = ?""",
        (tokens_in, tokens_out, cost, model, peak_context, first_context, last_context, context_window, request_count,
         cache_read_tokens_in, cache_write_tokens_in, uncached_tokens_in, session_id),
    )
    try:
        conn.execute(
            "UPDATE task_sessions SET telemetry_status = ? WHERE id = ?",
            (status, session_id),
        )
    except sqlite3.OperationalError:
        pass

    # Idle-gap-discounted active duration (issue #1069, schema 79). Written
    # separately and best-effort: new code may run against a pre-migration
    # schema mid-upgrade, and active_seconds is advisory observability data.
    active_seconds = totals.get("active_seconds")
    if isinstance(active_seconds, int):
        try:
            conn.execute(
                "UPDATE task_sessions SET active_seconds = ? WHERE id = ?",
                (active_seconds, session_id),
            )
        except sqlite3.OperationalError:
            pass


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
