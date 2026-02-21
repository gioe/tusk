#!/usr/bin/env python3
"""Manage acceptance criteria for tusk tasks.

Called by the tusk wrapper:
    tusk criteria add|list|done|reset ...

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — subcommand + flags
"""

import argparse
import glob as globmod
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


def load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return json.load(f)


def capture_criterion_cost(conn: sqlite3.Connection, criterion_id: int, task_id: int) -> None:
    """Best-effort: parse transcript window and store cost on the criterion row.

    Time window: from the previous criterion's completed_at (same task) or
    the active session's started_at, through to now.
    """
    try:
        lib.load_pricing()

        # Find window start: most recent committed_at (or completed_at) for same task
        prev = conn.execute(
            "SELECT COALESCE(committed_at, completed_at) AS window_ts "
            "FROM acceptance_criteria "
            "WHERE task_id = ? AND id <> ? AND completed_at IS NOT NULL "
            "ORDER BY completed_at DESC LIMIT 1",
            (task_id, criterion_id),
        ).fetchone()

        if prev and prev["window_ts"]:
            window_start = lib.parse_sqlite_timestamp(prev["window_ts"])
        else:
            # Fall back to most recent open session for this task
            session = conn.execute(
                "SELECT started_at FROM task_sessions "
                "WHERE task_id = ? ORDER BY started_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if session and session["started_at"]:
                window_start = lib.parse_sqlite_timestamp(session["started_at"])
            else:
                return  # No window start — skip cost tracking

        transcript_path = lib.find_transcript()
        if not transcript_path or not os.path.isfile(transcript_path):
            return

        totals = lib.aggregate_session(transcript_path, window_start, None)
        if totals["request_count"] == 0:
            return

        tokens_in = lib.compute_tokens_in(totals)
        tokens_out = totals["output_tokens"]
        cost = lib.compute_cost(totals)

        conn.execute(
            "UPDATE acceptance_criteria "
            "SET cost_dollars = ?, tokens_in = ?, tokens_out = ? "
            "WHERE id = ?",
            (cost, tokens_in, tokens_out, criterion_id),
        )
    except Exception:
        pass  # Best-effort — never block completion


# ── Verification ──────────────────────────────────────────────────────

def run_verification(criterion_type: str, spec: str) -> dict:
    """Run automated verification based on criterion type.

    Returns {"passed": bool, "output": str}.
    """
    if criterion_type == "manual" or not spec:
        return {"passed": True, "output": ""}

    if criterion_type in ("code", "test"):
        # Run spec as a shell command; pass means exit code 0
        try:
            result = subprocess.run(
                spec, shell=True, capture_output=True, text=True, timeout=120,
            )
            output = result.stdout.strip()
            if result.stderr.strip():
                output += ("\n" if output else "") + result.stderr.strip()
            # Truncate long output
            if len(output) > 2000:
                output = output[:2000] + "\n... (truncated)"
            return {"passed": result.returncode == 0, "output": output}
        except subprocess.TimeoutExpired:
            return {"passed": False, "output": "Verification timed out (120s)"}
        except Exception as e:
            return {"passed": False, "output": f"Error running verification: {e}"}

    if criterion_type == "file":
        # Check if file(s) matching the spec exist
        matches = globmod.glob(spec, recursive=True)
        if matches:
            file_list = ", ".join(matches[:10])
            if len(matches) > 10:
                file_list += f" ... ({len(matches)} total)"
            return {"passed": True, "output": f"Found: {file_list}"}
        return {"passed": False, "output": f"No files matching: {spec}"}

    return {"passed": False, "output": f"Unknown criterion type: {criterion_type}"}


SPEC_REQUIRED_TYPES = {"code", "test", "file"}


# ── Subcommands ──────────────────────────────────────────────────────

def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def cmd_add(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)

    # Verify task exists
    task = conn.execute("SELECT id FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if not task:
        print(f"Error: Task {args.task_id} not found", file=sys.stderr)
        conn.close()
        return 2

    # Validate criterion_type against config
    criterion_types = config.get("criterion_types", [])
    if criterion_types and args.type not in criterion_types:
        joined = ", ".join(criterion_types)
        print(f"Error: Invalid criterion type '{args.type}'. Valid: {joined}", file=sys.stderr)
        conn.close()
        return 2

    # Validate spec: required for non-manual types
    if args.type in SPEC_REQUIRED_TYPES and not args.spec:
        print(f"Error: --spec is required for criterion type '{args.type}'", file=sys.stderr)
        conn.close()
        return 2

    conn.execute(
        "INSERT INTO acceptance_criteria (task_id, criterion, source, criterion_type, verification_spec) "
        "VALUES (?, ?, ?, ?, ?)",
        (args.task_id, args.text, args.source, args.type, args.spec),
    )
    conn.commit()

    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    type_suffix = f" (type: {args.type})" if args.type != "manual" else ""
    print(f"Added criterion #{cid} to task #{args.task_id}{type_suffix}")
    return 0


def cmd_list(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)

    # Verify task exists
    task = conn.execute(
        "SELECT id, summary FROM tasks WHERE id = ?", (args.task_id,)
    ).fetchone()
    if not task:
        print(f"Error: Task {args.task_id} not found", file=sys.stderr)
        conn.close()
        return 2

    rows = conn.execute(
        "SELECT id, criterion, source, is_completed, is_deferred, deferred_reason, "
        "cost_dollars, tokens_in, tokens_out, "
        "criterion_type, verification_spec, commit_hash, committed_at, created_at "
        "FROM acceptance_criteria WHERE task_id = ? ORDER BY id",
        (args.task_id,),
    ).fetchall()
    conn.close()

    if not rows:
        print(f"No acceptance criteria for task #{args.task_id}: {task['summary']}")
        return 0

    print(f"Acceptance criteria for task #{args.task_id}: {task['summary']}")
    print(f"{'ID':<6} {'Done':<6} {'Type':<8} {'Source':<14} {'Cost':<10} {'Commit':<10} {'Committed At':<22} {'Criterion'}")
    print("-" * 122)
    total_cost = 0.0
    for r in rows:
        if r["is_completed"]:
            marker = "[x]"
        elif r["is_deferred"]:
            marker = "[~]"
        else:
            marker = "[ ]"
        cost_str = f"${r['cost_dollars']:.4f}" if r["cost_dollars"] else ""
        if r["cost_dollars"]:
            total_cost += r["cost_dollars"]
        ctype = r["criterion_type"] or "manual"
        commit_str = r["commit_hash"] or ""
        committed_str = r["committed_at"] or ""
        if len(committed_str) > 19:
            committed_str = committed_str[:19]
        criterion_text = r["criterion"]
        if r["is_deferred"] and r["deferred_reason"]:
            criterion_text += f" [deferred: {r['deferred_reason']}]"
        print(f"{r['id']:<6} {marker:<6} {ctype:<8} {r['source']:<14} {cost_str:<10} {commit_str:<10} {committed_str:<22} {criterion_text}")

    done = sum(1 for r in rows if r["is_completed"])
    deferred = sum(1 for r in rows if r["is_deferred"] and not r["is_completed"])
    summary = f"\nProgress: {done}/{len(rows)}"
    if deferred:
        summary += f"  |  Deferred: {deferred}"
    if total_cost > 0:
        summary += f"  |  Total cost: ${total_cost:.4f}"
    print(summary)
    return 0


def cmd_done(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)

    row = conn.execute(
        "SELECT id, task_id, criterion, is_completed, criterion_type, verification_spec "
        "FROM acceptance_criteria WHERE id = ?",
        (args.criterion_id,),
    ).fetchone()
    if not row:
        print(f"Error: Criterion {args.criterion_id} not found", file=sys.stderr)
        conn.close()
        return 2

    if row["is_completed"]:
        print(f"Criterion #{args.criterion_id} is already completed")
        conn.close()
        return 0

    criterion_type = row["criterion_type"] or "manual"
    spec = row["verification_spec"]

    # Run verification for non-manual types (unless --skip-verify)
    verification_result = None
    if criterion_type != "manual" and spec and not args.skip_verify:
        result = run_verification(criterion_type, spec)
        verification_result = json.dumps(result)

        if not result["passed"]:
            # Store the failed result
            conn.execute(
                "UPDATE acceptance_criteria SET verification_result = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (verification_result, args.criterion_id),
            )
            conn.commit()
            conn.close()

            print(f"Verification FAILED for criterion #{args.criterion_id} ({criterion_type}):",
                  file=sys.stderr)
            if result["output"]:
                print(result["output"], file=sys.stderr)
            print("Use --skip-verify to bypass verification.", file=sys.stderr)
            return 1

    # Best-effort: capture current git HEAD short hash and commit timestamp
    commit_hash = None
    committed_at = None
    try:
        commit_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip() or None
        if commit_hash:
            committed_at = subprocess.check_output(
                ["git", "log", "-1", "--format=%cI", "HEAD"],
                stderr=subprocess.DEVNULL,
            ).decode().strip() or None
    except Exception:
        pass  # Non-git environment — leave as NULL

    # Warn if another completed criterion on this task already has this commit hash
    if commit_hash is not None:
        dup = conn.execute(
            "SELECT id FROM acceptance_criteria "
            "WHERE task_id = ? AND id <> ? AND commit_hash = ? AND is_completed = 1 "
            "LIMIT 1",
            (row["task_id"], args.criterion_id, commit_hash),
        ).fetchone()
        if dup:
            print(
                f"Warning: Criterion #{args.criterion_id} shares commit {commit_hash} "
                f"with criterion #{dup['id']}.\n"
                f"Commit separately per criterion for accurate cost attribution.",
                file=sys.stderr,
            )

    conn.execute(
        "UPDATE acceptance_criteria SET is_completed = 1, "
        "completed_at = strftime('%Y-%m-%d %H:%M:%f', 'now'), "
        "commit_hash = ?, committed_at = ?, "
        "verification_result = ?, updated_at = datetime('now') WHERE id = ?",
        (commit_hash, committed_at, verification_result, args.criterion_id),
    )
    conn.commit()

    # Best-effort cost capture
    capture_criterion_cost(conn, args.criterion_id, row["task_id"])
    conn.commit()

    conn.close()
    verified_msg = ""
    if criterion_type != "manual" and not args.skip_verify:
        verified_msg = " (verification passed)"
    elif criterion_type != "manual" and args.skip_verify:
        verified_msg = " (verification skipped)"
    print(f"Criterion #{args.criterion_id} marked done{verified_msg}: {row['criterion']}")
    return 0


def cmd_skip(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)

    row = conn.execute(
        "SELECT id, task_id, criterion, is_completed, is_deferred, deferred_reason "
        "FROM acceptance_criteria WHERE id = ?",
        (args.criterion_id,),
    ).fetchone()
    if not row:
        print(f"Error: Criterion {args.criterion_id} not found", file=sys.stderr)
        conn.close()
        return 2

    if row["is_completed"]:
        print(f"Criterion #{args.criterion_id} is already completed")
        conn.close()
        return 0

    if row["is_deferred"]:
        print(
            f"Criterion #{args.criterion_id} is already deferred "
            f"(reason: {row['deferred_reason']}): {row['criterion']}"
        )
        conn.close()
        return 0

    conn.execute(
        "UPDATE acceptance_criteria SET is_deferred = 1, deferred_reason = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (args.reason, args.criterion_id),
    )
    conn.commit()
    conn.close()
    print(f"Criterion #{args.criterion_id} marked as deferred (reason: {args.reason}): {row['criterion']}")
    return 0


def cmd_reset(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)

    row = conn.execute(
        "SELECT id, task_id, criterion, is_completed, is_deferred "
        "FROM acceptance_criteria WHERE id = ?",
        (args.criterion_id,),
    ).fetchone()
    if not row:
        print(f"Error: Criterion {args.criterion_id} not found", file=sys.stderr)
        conn.close()
        return 2

    if not row["is_completed"] and not row["is_deferred"]:
        print(f"Criterion #{args.criterion_id} is already incomplete and not deferred")
        conn.close()
        return 0

    conn.execute(
        "UPDATE acceptance_criteria SET is_completed = 0, completed_at = NULL, "
        "cost_dollars = NULL, tokens_in = NULL, tokens_out = NULL, "
        "verification_result = NULL, commit_hash = NULL, committed_at = NULL, "
        "is_deferred = 0, deferred_reason = NULL, "
        "updated_at = datetime('now') WHERE id = ?",
        (args.criterion_id,),
    )
    conn.commit()
    conn.close()
    print(f"Criterion #{args.criterion_id} reset to incomplete: {row['criterion']}")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print("Usage: tusk criteria {add|list|done|skip|reset} ...", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    config_path = sys.argv[2]
    config = load_config(config_path)

    parser = argparse.ArgumentParser(
        prog="tusk criteria",
        description="Manage acceptance criteria for tasks",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # add
    add_p = subparsers.add_parser("add", help="Add a criterion to a task")
    add_p.add_argument("task_id", type=int, help="Task ID")
    add_p.add_argument("text", help="Criterion text")
    add_p.add_argument(
        "--source", default="original",
        choices=["original", "subsumption", "pr_review"],
        help="Source of the criterion (default: original)",
    )
    add_p.add_argument(
        "--type", default="manual",
        help="Criterion type (default: manual)",
    )
    add_p.add_argument(
        "--spec",
        help="Verification spec (required for non-manual types)",
    )

    # list
    list_p = subparsers.add_parser("list", help="List criteria for a task")
    list_p.add_argument("task_id", type=int, help="Task ID")

    # done
    done_p = subparsers.add_parser("done", help="Mark a criterion as completed")
    done_p.add_argument("criterion_id", type=int, help="Criterion ID")
    done_p.add_argument(
        "--skip-verify", action="store_true",
        help="Skip automated verification for non-manual criteria",
    )

    # skip
    skip_p = subparsers.add_parser("skip", help="Mark a criterion as deferred to chain orchestrator")
    skip_p.add_argument("criterion_id", type=int, help="Criterion ID")
    skip_p.add_argument(
        "--reason", required=True,
        help="Reason for deferral (e.g., 'chain' when handled by chain orchestrator)",
    )

    # reset
    reset_p = subparsers.add_parser("reset", help="Reset a criterion to incomplete (clears deferred flag too)")
    reset_p.add_argument("criterion_id", type=int, help="Criterion ID")

    args = parser.parse_args(sys.argv[3:])

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        handlers = {
            "add": cmd_add, "list": cmd_list, "done": cmd_done,
            "skip": cmd_skip, "reset": cmd_reset,
        }
        sys.exit(handlers[args.command](args, db_path, config))
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
