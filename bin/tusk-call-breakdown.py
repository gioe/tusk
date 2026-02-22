#!/usr/bin/env python3
"""Per-tool-call cost attribution for tusk.

Analyzes Claude Code transcripts to attribute cost to individual tool calls,
grouped by tool type.  Results are displayed as a sorted aggregate table and
optionally written to the tool_call_stats table.

Called by the tusk wrapper:
    tusk call-breakdown --task <id>
    tusk call-breakdown --session <id>
    tusk call-breakdown --skill-run <id>

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — flags

Flags:
    --task <id>       Aggregate all sessions for the given task
    --session <id>    Analyze a single session (writes to tool_call_stats)
    --skill-run <id>  Analyze a skill-run time window (display only)
    --write-only      Write to DB without printing the table (used by session-close)
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
        call_count, total_cost, max_cost, tokens_out
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
                }
            s = stats[tool]
            s["call_count"] += 1
            s["total_cost"] += item["cost"]
            s["max_cost"] = max(s["max_cost"], item["cost"])
            s["tokens_out"] += item["output_tokens"]
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

    header = f"{'Tool':<{col_w}}  {'Calls':>6}  {'Total Cost':>11}  {'Max Cost':>9}  {'Tokens Out':>11}"
    print(f"\nCall breakdown for {label}:")
    print(header)
    print("-" * len(header))
    for tool_name, s in sorted_tools:
        print(
            f"{tool_name:<{col_w}}  {s['call_count']:>6}  "
            f"${s['total_cost']:.6f}  ${s['max_cost']:.6f}  {s['tokens_out']:>11,}"
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
                   (session_id, task_id, tool_name, call_count, total_cost, max_cost, tokens_out, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(session_id, tool_name) DO UPDATE SET
                   call_count  = excluded.call_count,
                   total_cost  = excluded.total_cost,
                   max_cost    = excluded.max_cost,
                   tokens_out  = excluded.tokens_out,
                   computed_at = excluded.computed_at""",
            (
                session_id,
                task_id,
                tool_name,
                s["call_count"],
                round(s["total_cost"], 8),
                round(s["max_cost"], 8),
                s["tokens_out"],
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

    combined: dict[str, dict] = {}

    for row in rows:
        sid = row["id"]
        started_at = lib.parse_sqlite_timestamp(row["started_at"])
        ended_at = lib.parse_sqlite_timestamp(row["ended_at"]) if row["ended_at"] else None

        session_stats = aggregate_tool_calls(transcripts, started_at, ended_at)

        if session_stats:
            upsert_session_stats(conn, sid, task_id, session_stats)

        # Merge into combined for display
        for tool_name, s in session_stats.items():
            if tool_name not in combined:
                combined[tool_name] = {
                    "call_count": 0,
                    "total_cost": 0.0,
                    "max_cost": 0.0,
                    "tokens_out": 0,
                }
            c = combined[tool_name]
            c["call_count"] += s["call_count"]
            c["total_cost"] += s["total_cost"]
            c["max_cost"] = max(c["max_cost"], s["max_cost"])
            c["tokens_out"] += s["tokens_out"]

    if not write_only:
        print_table(combined, f"task {task_id} ({len(rows)} session(s))")


def cmd_skill_run(conn, run_id: int, transcripts: list[str]) -> None:
    """Analyze a skill-run time window (display only, no DB write)."""
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
    print_table(stats, f"skill-run {run_id} ({row['skill_name']})")


def main():
    if len(sys.argv) < 3:
        print(
            "Usage: tusk call-breakdown {--task <id> | --session <id> | --skill-run <id>} [--write-only]",
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
        elif args[i] == "--write-only":
            write_only = True
            i += 1
        else:
            print(f"Error: Unknown argument '{args[i]}'", file=sys.stderr)
            sys.exit(1)

    if task_id is None and session_id is None and run_id is None:
        print(
            "Usage: tusk call-breakdown {--task <id> | --session <id> | --skill-run <id>} [--write-only]",
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
            if write_only:
                print("Warning: --write-only has no effect with --skill-run (no DB write for skill runs).", file=sys.stderr)
            cmd_skill_run(conn, run_id, transcripts)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
