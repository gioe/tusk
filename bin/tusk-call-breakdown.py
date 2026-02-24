#!/usr/bin/env python3
"""Per-tool-call cost attribution for tusk.

Analyzes Claude Code transcripts to attribute cost to individual tool calls,
grouped by tool type.  Results are displayed as a sorted aggregate table and
optionally written to the tool_call_stats table.

Called by the tusk wrapper:
    tusk call-breakdown --task <id>
    tusk call-breakdown --session <id>
    tusk call-breakdown --skill-run <id>
    tusk call-breakdown --criterion <id>

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — flags

Flags:
    --task <id>       Aggregate all sessions for the given task
    --session <id>    Analyze a single session (writes to tool_call_stats)
    --skill-run <id>  Analyze a skill-run time window (writes to tool_call_stats)
    --criterion <id>  Recompute tool stats for a criterion's time window (writes to tool_call_stats)
    --write-only      Write to DB without printing the table (used by session-close / skill-run finish)
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
    """Return all JSONL transcript files for the project, newest first."""
    claude_dir = Path.home() / ".claude" / "projects" / project_hash
    if not claude_dir.is_dir():
        return []
    return sorted(
        [str(p) for p in claude_dir.glob("*.jsonl")],
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )


def aggregate_tool_calls(
    transcripts: list[str],
    started_at,
    ended_at,
) -> dict[str, dict]:
    """Collect tool call stats across all transcripts for a time window.

    Returns a dict keyed by tool_name with sub-keys:
        call_count, total_cost, max_cost, tokens_out, tokens_in
    """
    stats: dict[str, dict] = {}
    for transcript_path in transcripts:
        if not os.path.isfile(transcript_path):
            continue
        for item in lib.iter_tool_call_costs(transcript_path, started_at, ended_at):
            tool = item["tool_name"]
            if tool not in stats:
                stats[tool] = {
                    "call_count": 0,
                    "total_cost": 0.0,
                    "max_cost": 0.0,
                    "tokens_out": 0,
                    "tokens_in": 0,
                }
            s = stats[tool]
            s["call_count"] += 1
            s["total_cost"] += item["cost"]
            s["max_cost"] = max(s["max_cost"], item["cost"])
            s["tokens_out"] += item["output_tokens"]
            s["tokens_in"] += item["marginal_input_tokens"]
    return stats


def print_table(stats: dict[str, dict], label: str) -> None:
    """Print the aggregate tool-call cost table."""
    if not stats:
        print(f"No tool calls found for {label}.")
        return

    sorted_tools = sorted(stats.items(), key=lambda kv: kv[1]["total_cost"], reverse=True)
    total_cost = sum(s["total_cost"] for s in stats.values())
    total_calls = sum(s["call_count"] for s in stats.values())

    col_w = max(len(t) for t, _ in sorted_tools)
    col_w = max(col_w, 10)

    header = f"{'Tool':<{col_w}}  {'Calls':>6}  {'Total Cost':>11}  {'Max Cost':>9}  {'Tokens In':>10}  {'Tokens Out':>11}"
    print(f"\nCall breakdown for {label}:")
    print(header)
    print("-" * len(header))
    for tool_name, s in sorted_tools:
        print(
            f"{tool_name:<{col_w}}  {s['call_count']:>6}  "
            f"${s['total_cost']:.6f}  ${s['max_cost']:.6f}  "
            f"{s['tokens_in']:>10,}  {s['tokens_out']:>11,}"
        )
    print("-" * len(header))
    print(f"{'TOTAL':<{col_w}}  {total_calls:>6}  ${total_cost:.6f}")


def upsert_session_stats(
    conn: sqlite3.Connection,
    session_id: int,
    task_id: int | None,
    stats: dict[str, dict],
) -> None:
    """Write aggregated tool_call_stats rows for a session (upsert on UNIQUE conflict)."""
    for tool_name, s in stats.items():
        conn.execute(
            """INSERT INTO tool_call_stats
                   (session_id, task_id, tool_name, call_count, total_cost, max_cost, tokens_out, tokens_in, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(session_id, tool_name) DO UPDATE SET
                   call_count  = excluded.call_count,
                   total_cost  = excluded.total_cost,
                   max_cost    = excluded.max_cost,
                   tokens_out  = excluded.tokens_out,
                   tokens_in   = excluded.tokens_in,
                   computed_at = excluded.computed_at""",
            (
                session_id,
                task_id,
                tool_name,
                s["call_count"],
                round(s["total_cost"], 8),
                round(s["max_cost"], 8),
                s["tokens_out"],
                s["tokens_in"],
            ),
        )
    conn.commit()


def cmd_session(conn, session_id: int, transcripts: list[str], write_only: bool) -> None:
    """Analyze a single session and optionally write stats to DB."""
    row = conn.execute(
        "SELECT id, task_id, started_at, ended_at FROM task_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()

    if not row:
        print(f"Error: No session found with id {session_id}", file=sys.stderr)
        sys.exit(1)

    started_at = lib.parse_sqlite_timestamp(row["started_at"])
    ended_at = lib.parse_sqlite_timestamp(row["ended_at"]) if row["ended_at"] else None
    task_id = row["task_id"]

    if not transcripts:
        print("Warning: No transcripts found — tool_call_stats will be empty.", file=sys.stderr)
        return

    stats = aggregate_tool_calls(transcripts, started_at, ended_at)

    if not stats:
        print("Warning: No tool calls found in transcript for this session.", file=sys.stderr)
        return

    upsert_session_stats(conn, session_id, task_id, stats)

    if not write_only:
        print_table(stats, f"session {session_id}")


def _aggregate_sessions_single_pass(
    transcripts: list[str],
    sessions: list[tuple],
) -> dict[int, dict[str, dict]]:
    """Read each transcript once and route tool calls to the correct session window.

    sessions: list of (session_id, started_at, ended_at) tuples where started_at
    and ended_at are tz-aware datetimes (ended_at may be None for an open session).
    Must be non-empty.

    Returns a dict mapping session_id -> {tool_name -> stats dict}.

    Complexity: O(transcripts) file reads instead of O(sessions × transcripts).

    If two session windows overlap, a tool call in the overlap is attributed to the
    first matching session in the list (tie-breaking by list order). In practice,
    task sessions are sequential and non-overlapping.
    """
    if not sessions:
        return {}

    per_session: dict[int, dict[str, dict]] = {sid: {} for sid, _, _ in sessions}

    # Broad window: earliest session start to latest session end.
    # If any session is still open, use None (unbounded) as the upper bound.
    overall_start = min(s[1] for s in sessions)
    overall_end = (
        max(s[2] for s in sessions)
        if all(s[2] is not None for s in sessions)
        else None
    )

    for transcript_path in transcripts:
        if not os.path.isfile(transcript_path):
            continue
        for item in lib.iter_tool_call_costs(transcript_path, overall_start, overall_end):
            ts = item["ts"]
            # Route to the first matching session window.
            for sid, start, end in sessions:
                if ts >= start and (end is None or ts <= end):
                    stats = per_session[sid]
                    tool = item["tool_name"]
                    if tool not in stats:
                        stats[tool] = {
                            "call_count": 0,
                            "total_cost": 0.0,
                            "max_cost": 0.0,
                            "tokens_out": 0,
                            "tokens_in": 0,
                        }
                    s = stats[tool]
                    s["call_count"] += 1
                    s["total_cost"] += item["cost"]
                    s["max_cost"] = max(s["max_cost"], item["cost"])
                    s["tokens_out"] += item["output_tokens"]
                    s["tokens_in"] += item["marginal_input_tokens"]
                    break

    return per_session


def cmd_task(conn, task_id: int, transcripts: list[str], write_only: bool) -> None:
    """Analyze all sessions for a task, write per-session stats, display aggregate."""
    rows = conn.execute(
        "SELECT id, started_at, ended_at FROM task_sessions WHERE task_id = ? AND started_at IS NOT NULL",
        (task_id,),
    ).fetchall()

    if not rows:
        print(f"No sessions found for task {task_id}.")
        return

    if not transcripts:
        print("Warning: No transcripts found — cannot compute breakdown.", file=sys.stderr)
        return

    sessions = [
        (
            row["id"],
            lib.parse_sqlite_timestamp(row["started_at"]),
            lib.parse_sqlite_timestamp(row["ended_at"]) if row["ended_at"] else None,
        )
        for row in rows
    ]

    # Single pass: each transcript file is read once regardless of session count.
    per_session = _aggregate_sessions_single_pass(transcripts, sessions)

    combined: dict[str, dict] = {}

    for sid, _, _ in sessions:
        session_stats = per_session[sid]
        if session_stats:
            upsert_session_stats(conn, sid, task_id, session_stats)

        for tool_name, s in session_stats.items():
            if tool_name not in combined:
                combined[tool_name] = {
                    "call_count": 0,
                    "total_cost": 0.0,
                    "max_cost": 0.0,
                    "tokens_out": 0,
                    "tokens_in": 0,
                }
            c = combined[tool_name]
            c["call_count"] += s["call_count"]
            c["total_cost"] += s["total_cost"]
            c["max_cost"] = max(c["max_cost"], s["max_cost"])
            c["tokens_out"] += s["tokens_out"]
            c["tokens_in"] += s["tokens_in"]

    if not write_only:
        print_table(combined, f"task {task_id} ({len(rows)} session(s))")


def upsert_skill_run_stats(
    conn: sqlite3.Connection,
    run_id: int,
    stats: dict[str, dict],
) -> None:
    """Write aggregated tool_call_stats rows for a skill run (upsert on UNIQUE conflict)."""
    if not stats:
        return
    for tool_name, s in stats.items():
        conn.execute(
            """INSERT INTO tool_call_stats
                   (skill_run_id, tool_name, call_count, total_cost, max_cost, tokens_out, tokens_in, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(skill_run_id, tool_name) DO UPDATE SET
                   call_count  = excluded.call_count,
                   total_cost  = excluded.total_cost,
                   max_cost    = excluded.max_cost,
                   tokens_out  = excluded.tokens_out,
                   tokens_in   = excluded.tokens_in,
                   computed_at = excluded.computed_at""",
            (
                run_id,
                tool_name,
                s["call_count"],
                round(s["total_cost"], 8),
                round(s["max_cost"], 8),
                s["tokens_out"],
                s["tokens_in"],
            ),
        )
    conn.commit()


def cmd_skill_run(conn, run_id: int, transcripts: list[str], write_only: bool = False) -> None:
    """Analyze a skill-run time window, write stats to tool_call_stats, optionally display."""
    row = conn.execute(
        "SELECT id, skill_name, started_at, ended_at FROM skill_runs WHERE id = ?",
        (run_id,),
    ).fetchone()

    if not row:
        print(f"Error: No skill run found with id {run_id}", file=sys.stderr)
        sys.exit(1)

    if not row["started_at"]:
        print(f"Error: Skill run {run_id} has no started_at timestamp.", file=sys.stderr)
        sys.exit(1)

    started_at = lib.parse_sqlite_timestamp(row["started_at"])
    ended_at = lib.parse_sqlite_timestamp(row["ended_at"]) if row["ended_at"] else None

    if not transcripts:
        print("Warning: No transcripts found — cannot compute breakdown.", file=sys.stderr)
        return

    stats = aggregate_tool_calls(transcripts, started_at, ended_at)
    upsert_skill_run_stats(conn, run_id, stats)

    if not write_only:
        print_table(stats, f"skill-run {run_id} ({row['skill_name']})")


def upsert_criterion_stats(
    conn: sqlite3.Connection,
    criterion_id: int,
    task_id: int,
    stats: dict[str, dict],
    commit: bool = True,
) -> None:
    """Write aggregated tool_call_stats rows for a criterion (upsert on UNIQUE conflict)."""
    lib.upsert_criterion_tool_stats(conn, criterion_id, task_id, stats, commit=commit)


def cmd_criterion(conn, criterion_id: int, transcripts: list[str], write_only: bool) -> None:
    """Recompute tool stats for a criterion's time window and write to tool_call_stats."""
    row = conn.execute(
        "SELECT id, task_id, completed_at, commit_hash FROM acceptance_criteria WHERE id = ?",
        (criterion_id,),
    ).fetchone()

    if not row:
        print(f"Error: No criterion found with id {criterion_id}", file=sys.stderr)
        sys.exit(1)

    if not row["completed_at"]:
        print(
            f"Error: Criterion {criterion_id} is not yet completed — cannot recompute stats "
            "without a known end boundary.",
            file=sys.stderr,
        )
        sys.exit(1)

    task_id = row["task_id"]
    commit_hash = row["commit_hash"]

    # Detect shared-commit group: all completed criteria on this task with the same commit_hash.
    group_ids: list = []
    if commit_hash:
        group_rows = conn.execute(
            "SELECT id FROM acceptance_criteria "
            "WHERE task_id = ? AND commit_hash = ? AND is_completed = 1 "
            "ORDER BY COALESCE(committed_at, completed_at) ASC",
            (task_id, commit_hash),
        ).fetchall()
        group_ids = [r["id"] for r in group_rows]

    n = len(group_ids) if len(group_ids) > 1 else 1

    # Window start.
    # For a shared-commit group, exclude all group members from the boundary search
    # so the window spans the full work period for the entire group.
    if n > 1:
        prev = conn.execute(
            "SELECT COALESCE(committed_at, completed_at) AS window_ts "
            "FROM acceptance_criteria "
            "WHERE task_id = ? AND (commit_hash IS NULL OR commit_hash <> ?) "
            "AND completed_at IS NOT NULL "
            "ORDER BY COALESCE(committed_at, completed_at) DESC LIMIT 1",
            (task_id, commit_hash),
        ).fetchone()
    else:
        # Single-criterion: most recent prior criterion on the same task, ordered by the
        # effective timestamp (COALESCE(committed_at, completed_at)) to avoid overlap.
        prev = conn.execute(
            "SELECT COALESCE(committed_at, completed_at) AS window_ts "
            "FROM acceptance_criteria "
            "WHERE task_id = ? AND id <> ? AND completed_at IS NOT NULL "
            "ORDER BY COALESCE(committed_at, completed_at) DESC LIMIT 1",
            (task_id, criterion_id),
        ).fetchone()

    if prev and prev["window_ts"]:
        started_at = lib.parse_sqlite_timestamp(prev["window_ts"])
    else:
        # Fall back to the most recent session start for this task
        session = conn.execute(
            "SELECT started_at FROM task_sessions "
            "WHERE task_id = ? ORDER BY started_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if session and session["started_at"]:
            started_at = lib.parse_sqlite_timestamp(session["started_at"])
        else:
            print(
                f"Error: Cannot determine window start for criterion {criterion_id} "
                "(no prior criterion and no task session found).",
                file=sys.stderr,
            )
            sys.exit(1)

    # Window end.
    # For a shared-commit group, use the latest completed_at among all group members
    # as the window end so the full group's cost is captured.
    if n > 1:
        latest_row = conn.execute(
            "SELECT MAX(completed_at) AS max_ts FROM acceptance_criteria "
            "WHERE task_id = ? AND commit_hash = ? AND is_completed = 1",
            (task_id, commit_hash),
        ).fetchone()
        ended_at = (
            lib.parse_sqlite_timestamp(latest_row["max_ts"])
            if latest_row and latest_row["max_ts"]
            else lib.parse_sqlite_timestamp(row["completed_at"])
        )
    else:
        ended_at = lib.parse_sqlite_timestamp(row["completed_at"])

    if not transcripts:
        print("Warning: No transcripts found — cannot compute breakdown.", file=sys.stderr)
        return

    stats = aggregate_tool_calls(transcripts, started_at, ended_at)

    if not stats:
        print(
            f"Warning: No tool calls found in transcript for criterion {criterion_id}.",
            file=sys.stderr,
        )
        return

    # For a shared-commit group, split stats evenly across N members and update all of them.
    # Use commit=False so the tool_call_stats inserts and the AC cost UPDATE below land in
    # one atomic transaction — a single conn.commit() at the end covers both writes.
    if n > 1:
        for s in stats.values():
            s["call_count"] = s["call_count"] // n
            s["total_cost"] /= n
            s["max_cost"] /= n
            s["tokens_out"] //= n
            s["tokens_in"] //= n
        for gid in group_ids:
            upsert_criterion_stats(conn, gid, task_id, stats, commit=False)
    else:
        upsert_criterion_stats(conn, criterion_id, task_id, stats, commit=False)

    # Refresh acceptance_criteria cost columns to match the recomputed (and possibly split) stats.
    # This UPDATE and the tool_call_stats inserts above are committed together below.
    ac_cost = round(sum(s["total_cost"] for s in stats.values()), 8)
    ac_tokens_in = sum(s["tokens_in"] for s in stats.values())
    ac_tokens_out = sum(s["tokens_out"] for s in stats.values())
    ids_to_update = group_ids if n > 1 else [criterion_id]
    for gid in ids_to_update:
        conn.execute(
            "UPDATE acceptance_criteria "
            "SET cost_dollars = ?, tokens_in = ?, tokens_out = ? "
            "WHERE id = ?",
            (ac_cost, ac_tokens_in, ac_tokens_out, gid),
        )
    conn.commit()

    if not write_only:
        print_table(stats, f"criterion {criterion_id} (task {task_id})")


def main():
    if len(sys.argv) < 3:
        print(
            "Usage: tusk call-breakdown {--task <id> | --session <id> | --skill-run <id> | --criterion <id>} [--write-only]",
            file=sys.stderr,
        )
        sys.exit(1)

    db_path = sys.argv[1]
    # sys.argv[2] is config_path (unused here)
    args = sys.argv[3:]

    # Parse flags
    task_id: int | None = None
    session_id: int | None = None
    run_id: int | None = None
    criterion_id: int | None = None
    write_only = False

    i = 0
    while i < len(args):
        if args[i] == "--task":
            if i + 1 >= len(args):
                print("Error: --task requires an integer ID", file=sys.stderr)
                sys.exit(1)
            try:
                task_id = int(args[i + 1])
            except ValueError:
                print(f"Error: --task requires an integer, got '{args[i+1]}'", file=sys.stderr)
                sys.exit(1)
            i += 2
        elif args[i] == "--session":
            if i + 1 >= len(args):
                print("Error: --session requires an integer ID", file=sys.stderr)
                sys.exit(1)
            try:
                session_id = int(args[i + 1])
            except ValueError:
                print(f"Error: --session requires an integer, got '{args[i+1]}'", file=sys.stderr)
                sys.exit(1)
            i += 2
        elif args[i] == "--skill-run":
            if i + 1 >= len(args):
                print("Error: --skill-run requires an integer ID", file=sys.stderr)
                sys.exit(1)
            try:
                run_id = int(args[i + 1])
            except ValueError:
                print(f"Error: --skill-run requires an integer, got '{args[i+1]}'", file=sys.stderr)
                sys.exit(1)
            i += 2
        elif args[i] == "--criterion":
            if i + 1 >= len(args):
                print("Error: --criterion requires an integer ID", file=sys.stderr)
                sys.exit(1)
            try:
                criterion_id = int(args[i + 1])
            except ValueError:
                print(f"Error: --criterion requires an integer, got '{args[i+1]}'", file=sys.stderr)
                sys.exit(1)
            i += 2
        elif args[i] == "--write-only":
            write_only = True
            i += 1
        else:
            print(f"Error: Unknown argument '{args[i]}'", file=sys.stderr)
            sys.exit(1)

    if task_id is None and session_id is None and run_id is None and criterion_id is None:
        print(
            "Usage: tusk call-breakdown {--task <id> | --session <id> | --skill-run <id> | --criterion <id>} [--write-only]",
            file=sys.stderr,
        )
        sys.exit(1)

    lib.load_pricing()

    project_hash = lib.derive_project_hash(os.getcwd())
    transcripts = find_all_transcripts(project_hash)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        if session_id is not None:
            cmd_session(conn, session_id, transcripts, write_only)
        elif task_id is not None:
            cmd_task(conn, task_id, transcripts, write_only)
        elif run_id is not None:
            cmd_skill_run(conn, run_id, transcripts, write_only)
        elif criterion_id is not None:
            cmd_criterion(conn, criterion_id, transcripts, write_only)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
