#!/usr/bin/env python3
"""Skill run cost tracking for tusk.

Records start/end timestamps for skill executions and computes cost from
the Claude Code JSONL transcript for the time window.

Called by the tusk wrapper:
    tusk skill-run start <skill_name>
    tusk skill-run finish <run_id> [--metadata '{"key":"val"}']
    tusk skill-run list [<skill_name>] [--limit N]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — subcommand + args
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
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


def cmd_start(conn, skill_name: str) -> None:
    """Insert a new skill_runs row and print the run_id."""
    cur = conn.execute(
        "INSERT INTO skill_runs (skill_name) VALUES (?)",
        (skill_name,),
    )
    conn.commit()
    run_id = cur.lastrowid

    row = conn.execute(
        "SELECT id, skill_name, started_at FROM skill_runs WHERE id = ?",
        (run_id,),
    ).fetchone()

    print(json.dumps({"run_id": row["id"], "started_at": row["started_at"]}))


def cmd_finish(conn, run_id: int, metadata: str | None, db_path: str) -> None:
    """Set ended_at, parse transcript, compute cost, update row, print summary."""
    row = conn.execute(
        "SELECT id, skill_name, started_at, ended_at FROM skill_runs WHERE id = ?",
        (run_id,),
    ).fetchone()

    if not row:
        print(f"Error: No skill run found with id {run_id}", file=sys.stderr)
        sys.exit(1)

    if row["ended_at"]:
        print(f"Warning: Run {run_id} is already finished (ended_at={row['ended_at']})", file=sys.stderr)

    # Set ended_at
    conn.execute(
        "UPDATE skill_runs SET ended_at = datetime('now') WHERE id = ?",
        (run_id,),
    )
    conn.commit()

    # Re-fetch with ended_at populated
    row = conn.execute(
        "SELECT id, skill_name, started_at, ended_at FROM skill_runs WHERE id = ?",
        (run_id,),
    ).fetchone()

    lib.load_pricing()

    started_at = lib.parse_sqlite_timestamp(row["started_at"])
    ended_at = lib.parse_sqlite_timestamp(row["ended_at"])

    # Discover transcript
    transcript_path = lib.find_transcript(lib.derive_project_hash(os.getcwd()))

    cost = 0.0
    tokens_in = 0
    tokens_out = 0
    model = ""
    request_count = 0

    if transcript_path and os.path.isfile(transcript_path):
        totals = lib.aggregate_session(transcript_path, started_at, ended_at)
        if totals["request_count"] > 0:
            cost = lib.compute_cost(totals)
            tokens_in = lib.compute_tokens_in(totals)
            tokens_out = totals["output_tokens"]
            model = totals["model"]
            request_count = totals["request_count"]
    else:
        print(
            "Warning: No transcript found — cost will be $0.00.",
            file=sys.stderr,
        )

    conn.execute(
        """UPDATE skill_runs
           SET cost_dollars = ?, tokens_in = ?, tokens_out = ?, model = ?, metadata = ?
           WHERE id = ?""",
        (cost, tokens_in, tokens_out, model, metadata, run_id),
    )
    conn.commit()
    # Close connection before spawning subprocess to avoid SQLITE_BUSY (two write
    # connections to the same DB file under the default journal mode).
    conn.close()

    # Persist per-tool-call cost breakdown for this skill run
    try:
        result = subprocess.run(
            ["tusk", "call-breakdown", "--skill-run", str(run_id), "--write-only"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            msg = result.stderr.strip() or f"exit code {result.returncode}"
            print(f"Warning: call-breakdown failed: {msg}", file=sys.stderr)
    except FileNotFoundError:
        print("Warning: 'tusk' not found — tool call breakdown not persisted.", file=sys.stderr)

    print(f"Skill run {run_id} ({row['skill_name']}) finished:")
    print(f"  Model:         {model or '(unknown)'}")
    print(f"  Requests:      {request_count}")
    print(f"  Tokens in:     {tokens_in:,}")
    print(f"  Tokens out:    {tokens_out:,}")
    print(f"  Est. cost:     ${cost:.4f}")
    if metadata:
        print(f"  Metadata:      {metadata}")


def cmd_list(conn, skill_name: str | None, limit: int) -> None:
    """Print recent skill runs, optionally filtered by skill name."""
    if skill_name:
        rows = conn.execute(
            """SELECT id, skill_name, started_at, ended_at,
                      cost_dollars, tokens_in, tokens_out, model, metadata
               FROM skill_runs
               WHERE skill_name = ?
               ORDER BY id DESC
               LIMIT ?""",
            (skill_name, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, skill_name, started_at, ended_at,
                      cost_dollars, tokens_in, tokens_out, model, metadata
               FROM skill_runs
               ORDER BY id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

    if not rows:
        print("No skill runs recorded yet.")
        return

    # Header
    print(f"{'ID':<6} {'Skill':<20} {'Started':<20} {'Cost':>8}  {'Tokens In':>10}  {'Model':<25}  Metadata")
    print("-" * 100)
    for r in rows:
        cost_str = f"${r['cost_dollars']:.4f}" if r["cost_dollars"] is not None else "pending"
        tokens_str = f"{r['tokens_in']:,}" if r["tokens_in"] is not None else "-"
        meta_str = r["metadata"] or ""
        started = (r["started_at"] or "")[:16]
        status = "(open)" if not r["ended_at"] else ""
        print(f"{r['id']:<6} {r['skill_name']:<20} {started:<20} {cost_str:>8}  {tokens_str:>10}  {(r['model'] or '-'):<25}  {meta_str} {status}")


def main():
    if len(sys.argv) < 4:
        print(
            "Usage: tusk skill-run {start <skill_name> | finish <run_id> [--metadata JSON] | list [<skill_name>] [--limit N]}",
            file=sys.stderr,
        )
        sys.exit(1)

    db_path = sys.argv[1]
    # sys.argv[2] is config_path (unused here)
    args = sys.argv[3:]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        subcommand = args[0]

        if subcommand == "start":
            if len(args) < 2:
                print("Usage: tusk skill-run start <skill_name>", file=sys.stderr)
                sys.exit(1)
            cmd_start(conn, args[1])

        elif subcommand == "finish":
            if len(args) < 2:
                print("Usage: tusk skill-run finish <run_id> [--metadata JSON]", file=sys.stderr)
                sys.exit(1)
            try:
                run_id = int(args[1])
            except ValueError:
                print(f"Error: run_id must be an integer, got '{args[1]}'", file=sys.stderr)
                sys.exit(1)
            # Parse optional --metadata flag
            metadata = None
            i = 2
            while i < len(args):
                if args[i] == "--metadata" and i + 1 < len(args):
                    metadata = args[i + 1]
                    i += 2
                else:
                    i += 1
            cmd_finish(conn, run_id, metadata, db_path)

        elif subcommand == "list":
            skill_filter = None
            limit = 20
            i = 1
            while i < len(args):
                if args[i] == "--limit" and i + 1 < len(args):
                    limit = int(args[i + 1])
                    i += 2
                elif not args[i].startswith("--"):
                    skill_filter = args[i]
                    i += 1
                else:
                    i += 1
            cmd_list(conn, skill_filter, limit)

        else:
            print(f"Error: unknown subcommand '{subcommand}'. Use start, finish, or list.", file=sys.stderr)
            sys.exit(1)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
