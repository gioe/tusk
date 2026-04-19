#!/usr/bin/env python3
"""Pre-aggregated retro signals for a single task, emitted as one compact JSON blob.

Called by the tusk wrapper:
    tusk retro-signals <task_id>

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3] — task_id (integer or TASK-NNN prefix form)

Output JSON shape:
    {
        "task_id": N,
        "complexity": "M" | null,
        "reopen_count": N,
        "rework_chain": {
            "fixes":    [{"id": N, "summary": "...", "status": "..."}, ...],
            "fixed_by": [{"id": N, "summary": "...", "status": "..."}, ...]
        },
        "review_themes":       [{"category": "...", "severity": "...",
                                 "count": N, "sample": "..."}, ...],
        "deferred_review_comments": [{"id": N, "category": "...", "severity": "...",
                                      "file_path": "..." | null,
                                      "deferred_task_id": N | null,
                                      "sample": "..."}, ...],
        "skipped_criteria":    [{"id": N, "criterion": "...",
                                 "is_deferred": 0|1, "skip_note": "..."}, ...],
        "tool_call_outliers":  [{"tool_name": "...", "call_count": N,
                                 "total_cost": N.N, "threshold": N,
                                 "complexity": "..." | null}, ...],
        "tool_errors":         [{"tool_name": "...", "error_count": N,
                                 "sample": "..."}, ...],
        "unconsumed_next_steps": [{"created_at": "...", "next_steps": "..."}, ...]
    }

Every signal field is always present; empty signals are zero counts or empty arrays
so callers can iterate unconditionally.

Exit codes:
    0 — success
    1 — error (bad arguments, task not found, DB issue)
"""

import argparse
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # noqa: E402

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_pricing_lib = tusk_loader.load("tusk-pricing-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection


# Per-complexity call_count thresholds for tool_call_outliers. Tunable; defaults
# chosen to roughly match observed P90 call counts per complexity bucket.
# `None` key applies to tasks with no complexity set.
CALL_COUNT_THRESHOLDS: dict = {
    "XS": 20,
    "S": 40,
    "M": 80,
    "L": 150,
    "XL": 300,
    None: 80,
}

# Minimum recurrence for a review (category, severity) pair to count as a theme.
REVIEW_THEME_MIN_RECURRENCE = 2

# Max chars of a representative review comment to include (keeps output compact).
REVIEW_SAMPLE_MAX_CHARS = 80

# Max chars of a representative tool-error message to include.
TOOL_ERROR_SAMPLE_MAX_CHARS = 160


def _resolve_task_id(raw: str) -> int:
    """Accept '5' or 'TASK-5' → 5. Raises ValueError on junk."""
    return int(re.sub(r"^TASK-", "", raw, flags=re.IGNORECASE))


def _compact(text: str, limit: int) -> str:
    """Strip and truncate a free-text field to `limit` chars (…-suffix if cut)."""
    s = (text or "").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def fetch_reopen_count(conn: sqlite3.Connection, task_id: int) -> int:
    """Count transitions back into the 'To Do' state for this task.

    Captures both mid-task (In Progress → To Do) and post-Done (Done → To Do)
    reopens, since task-reopen writes a transition row either way.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM task_status_transitions "
        "WHERE task_id = ? AND to_status = 'To Do'",
        (task_id,),
    ).fetchone()
    return int(row["c"]) if row else 0


def fetch_rework_chain(conn: sqlite3.Connection, task_id: int) -> dict:
    """Return {fixes, fixed_by} — both directions of the fixes_task_id FK."""
    fixes = conn.execute(
        "SELECT t.id, t.summary, t.status "
        "FROM tasks src JOIN tasks t ON t.id = src.fixes_task_id "
        "WHERE src.id = ?",
        (task_id,),
    ).fetchall()
    fixed_by = conn.execute(
        "SELECT id, summary, status FROM tasks "
        "WHERE fixes_task_id = ? ORDER BY created_at, id",
        (task_id,),
    ).fetchall()
    return {
        "fixes": [{"id": r["id"], "summary": r["summary"], "status": r["status"]} for r in fixes],
        "fixed_by": [
            {"id": r["id"], "summary": r["summary"], "status": r["status"]} for r in fixed_by
        ],
    }


def fetch_review_themes(conn: sqlite3.Connection, task_id: int) -> list[dict]:
    """(category, severity) pairs with count >= REVIEW_THEME_MIN_RECURRENCE across
    all review passes for the task, plus one representative short sample per pair.

    Aggregation (GROUP BY / HAVING) runs in SQL; the sample is selected via a
    correlated subquery so we never pull full comment bodies into Python."""
    rows = conn.execute(
        """
        SELECT rc.category,
               rc.severity,
               COUNT(*) AS cnt,
               (SELECT comment FROM review_comments rc2
                  JOIN code_reviews cr2 ON cr2.id = rc2.review_id
                 WHERE cr2.task_id = ?
                   AND rc2.category IS rc.category
                   AND rc2.severity IS rc.severity
                 ORDER BY rc2.id
                 LIMIT 1) AS sample_comment
          FROM review_comments rc
          JOIN code_reviews cr ON cr.id = rc.review_id
         WHERE cr.task_id = ?
         GROUP BY rc.category, rc.severity
        HAVING COUNT(*) >= ?
         ORDER BY cnt DESC, rc.category, rc.severity
        """,
        (task_id, task_id, REVIEW_THEME_MIN_RECURRENCE),
    ).fetchall()
    return [
        {
            "category": r["category"],
            "severity": r["severity"],
            "count": int(r["cnt"]),
            "sample": _compact(r["sample_comment"] or "", REVIEW_SAMPLE_MAX_CHARS),
        }
        for r in rows
    ]


def fetch_deferred_review_comments(conn: sqlite3.Connection, task_id: int) -> list[dict]:
    """Review comments the reviewer marked resolution='deferred' across all
    review passes for this task, each with the follow-up task it was punted to.

    Emitted individually (not grouped) because each row is a concrete open thread
    — /retro reports them alongside their deferred_task_id so the audit trail of
    'why wasn't this fixed' stays visible. Comment bodies are truncated via the
    same sample limit as review_themes to keep the blob compact."""
    rows = conn.execute(
        """
        SELECT rc.id,
               rc.category,
               rc.severity,
               rc.file_path,
               rc.deferred_task_id,
               rc.comment
          FROM review_comments rc
          JOIN code_reviews cr ON cr.id = rc.review_id
         WHERE cr.task_id = ?
           AND rc.resolution = 'deferred'
         ORDER BY rc.id
        """,
        (task_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "category": r["category"],
            "severity": r["severity"],
            "file_path": r["file_path"],
            "deferred_task_id": r["deferred_task_id"],
            "sample": _compact(r["comment"] or "", REVIEW_SAMPLE_MAX_CHARS),
        }
        for r in rows
    ]


def fetch_skipped_criteria(conn: sqlite3.Connection, task_id: int) -> list[dict]:
    """Criteria with a recorded rationale for not shipping code — covers
    skip-verify closures (skip_note, from `tusk criteria done --skip-verify`)
    and explicit deferrals (deferred_reason, from `tusk criteria skip --reason`).
    The two paths write to different columns; either populated column surfaces
    the criterion. The output 'skip_note' key is COALESCE'd (skip_note wins)
    so FULL-RETRO.md's renderer sees one field regardless of origin."""
    rows = conn.execute(
        "SELECT id, criterion, is_deferred, "
        "       COALESCE(NULLIF(TRIM(skip_note), ''), deferred_reason) AS skip_note "
        "  FROM acceptance_criteria "
        " WHERE task_id = ? "
        "   AND ((skip_note IS NOT NULL AND TRIM(skip_note) <> '') "
        "        OR (is_deferred = 1 AND deferred_reason IS NOT NULL "
        "            AND TRIM(deferred_reason) <> '')) "
        " ORDER BY id",
        (task_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "criterion": r["criterion"],
            "is_deferred": int(r["is_deferred"] or 0),
            "skip_note": r["skip_note"],
        }
        for r in rows
    ]


def fetch_tool_call_outliers(
    conn: sqlite3.Connection, task_id: int, complexity: str | None
) -> list[dict]:
    """Tools whose SUM(call_count) across this task's sessions exceeds the
    per-complexity threshold.

    tool_call_stats is denormalized across (session, skill_run, criterion) grains,
    so filtering by session grain avoids cross-grain double-counting. Aggregation
    and the threshold filter both run in SQL via HAVING."""
    threshold = CALL_COUNT_THRESHOLDS.get(complexity, CALL_COUNT_THRESHOLDS[None])
    rows = conn.execute(
        """
        SELECT tool_name,
               SUM(call_count) AS total_calls,
               SUM(total_cost) AS total_cost
          FROM tool_call_stats
         WHERE session_id IN (SELECT id FROM task_sessions WHERE task_id = ?)
         GROUP BY tool_name
        HAVING SUM(call_count) >= ?
         ORDER BY total_calls DESC, tool_name
        """,
        (task_id, threshold),
    ).fetchall()
    return [
        {
            "tool_name": r["tool_name"],
            "call_count": int(r["total_calls"] or 0),
            "total_cost": float(r["total_cost"] or 0.0),
            "threshold": threshold,
            "complexity": complexity,
        }
        for r in rows
    ]


def fetch_tool_errors(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    transcripts: list[str] | None = None,
) -> list[dict]:
    """Tool failures observed during any of this task's sessions, aggregated
    per tool_name.

    Data source is Claude Code transcripts (`~/.claude/projects/*.jsonl`), not
    a dedicated hook or log file — every failing tool_use already lands in the
    transcript with `is_error: true`. See docs/retro-error-detection.md for
    the rationale.

    Returns a list of `{tool_name, error_count, sample}` dicts sorted by
    `error_count` descending then `tool_name`, so the reviewer sees the
    noisiest tool first. `sample` is the text of the first error observed for
    that tool (trimmed to TOOL_ERROR_SAMPLE_MAX_CHARS) — enough to recognize
    the failure mode without dumping full stderr into the retro.

    The `transcripts` argument is injectable for tests; production callers
    resolve transcripts via `find_all_transcripts_with_fallback()`.
    """
    sessions = conn.execute(
        "SELECT started_at, ended_at FROM task_sessions "
        "  WHERE task_id = ? AND started_at IS NOT NULL "
        "  ORDER BY started_at",
        (task_id,),
    ).fetchall()
    if not sessions:
        return []

    if transcripts is None:
        transcripts = _pricing_lib.find_all_transcripts_with_fallback()
    if not transcripts:
        return []

    # Parse each session's window into tz-aware datetimes up front.
    windows: list[tuple] = []
    for row in sessions:
        try:
            start = _pricing_lib.parse_sqlite_timestamp(row["started_at"])
        except (ValueError, TypeError):
            continue
        end = None
        if row["ended_at"]:
            try:
                end = _pricing_lib.parse_sqlite_timestamp(row["ended_at"])
            except (ValueError, TypeError):
                end = None
        windows.append((start, end))

    if not windows:
        return []

    # Broad window: earliest start to latest end. An open session (ended_at
    # IS NULL) pushes the upper bound to None so we scan to end-of-transcript.
    overall_start = min(w[0] for w in windows)
    overall_end = (
        max(w[1] for w in windows) if all(w[1] is not None for w in windows) else None
    )

    per_tool: dict[str, dict] = {}
    for transcript_path in transcripts:
        if not os.path.isfile(transcript_path):
            continue
        try:
            iterator = _pricing_lib.iter_tool_errors(
                transcript_path, overall_start, overall_end
            )
        except OSError:
            continue
        for item in iterator:
            ts = item["ts"]
            # Only count errors that fall inside one of the task's sessions —
            # the broad window lets us read each transcript once, but the
            # per-session filter prevents cross-session bleed when the task
            # ran in multiple non-contiguous sittings.
            if not any(
                ts >= start and (end is None or ts <= end) for start, end in windows
            ):
                continue
            tool_name = item["tool_name"]
            bucket = per_tool.setdefault(
                tool_name, {"error_count": 0, "sample": None}
            )
            bucket["error_count"] += 1
            if bucket["sample"] is None and item["error_text"]:
                bucket["sample"] = _compact(
                    item["error_text"], TOOL_ERROR_SAMPLE_MAX_CHARS
                )

    rows = [
        {
            "tool_name": tool_name,
            "error_count": info["error_count"],
            "sample": info["sample"] or "",
        }
        for tool_name, info in per_tool.items()
    ]
    rows.sort(key=lambda r: (-r["error_count"], r["tool_name"]))
    return rows


def fetch_unconsumed_next_steps(conn: sqlite3.Connection, task_id: int) -> list[dict]:
    """Non-empty next_steps handoff notes from task_progress, oldest first.

    We emit all of them rather than guessing which were 'consumed' by downstream
    work — /retro eyeballs the sequence to spot drift or abandoned threads."""
    rows = conn.execute(
        "SELECT created_at, next_steps FROM task_progress "
        " WHERE task_id = ? "
        "   AND next_steps IS NOT NULL "
        "   AND TRIM(next_steps) <> '' "
        " ORDER BY created_at, id",
        (task_id,),
    ).fetchall()
    return [{"created_at": r["created_at"], "next_steps": r["next_steps"]} for r in rows]


def build_signals(conn: sqlite3.Connection, task_id: int) -> dict:
    """Fetch every signal and bundle into the output dict. Callers handle
    task-not-found separately so this function always returns populated keys."""
    task_row = conn.execute(
        "SELECT complexity FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    complexity = task_row["complexity"] if task_row else None
    return {
        "task_id": task_id,
        "complexity": complexity,
        "reopen_count": fetch_reopen_count(conn, task_id),
        "rework_chain": fetch_rework_chain(conn, task_id),
        "review_themes": fetch_review_themes(conn, task_id),
        "deferred_review_comments": fetch_deferred_review_comments(conn, task_id),
        "skipped_criteria": fetch_skipped_criteria(conn, task_id),
        "tool_call_outliers": fetch_tool_call_outliers(conn, task_id, complexity),
        "tool_errors": fetch_tool_errors(conn, task_id),
        "unconsumed_next_steps": fetch_unconsumed_next_steps(conn, task_id),
    }


def main(argv: list) -> int:
    db_path = argv[0]
    # argv[1] is config_path — reserved for future use
    parser = argparse.ArgumentParser(
        prog="tusk retro-signals",
        description="Fetch pre-aggregated retro signals for a task as one JSON blob.",
    )
    parser.add_argument("task_id", help="Task ID (integer or TASK-NNN prefix form)")
    args = parser.parse_args(argv[2:])

    try:
        task_id = _resolve_task_id(args.task_id)
    except ValueError:
        print(f"Invalid task ID: {args.task_id}", file=sys.stderr)
        return 1

    conn = get_connection(db_path)
    try:
        if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
            print(f"Task {task_id} not found", file=sys.stderr)
            return 1
        print(dumps(build_signals(conn, task_id)))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk retro-signals <task_id>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
