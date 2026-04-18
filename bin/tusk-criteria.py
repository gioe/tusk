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
import json
import os
import sqlite3
import subprocess
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

lib = tusk_loader.load("tusk-pricing-lib")
_db_lib = tusk_loader.load("tusk-db-lib")
get_connection = _db_lib.get_connection
load_config = _db_lib.load_config


def capture_criterion_cost(conn: sqlite3.Connection, criterion_id: int, task_id: int, completed_at=None) -> None:
    """Best-effort: parse transcript window and store cost on the criterion row.

    Time window: from the previous criterion's completed_at (same task) or
    the active session's started_at, through to completed_at (bounded window).

    When N criteria share the same commit_hash (completed in one tusk commit call),
    cost is split evenly across all N so each gets a proportional estimate.
    The split is applied to all N criteria on every call, so the last criterion
    processed in the batch sets the final (correct) values for all group members.
    """
    try:
        lib.load_pricing()

        # Detect shared-commit group: all completed criteria on this task with the same commit_hash.
        crit_row = conn.execute(
            "SELECT commit_hash FROM acceptance_criteria WHERE id = ?",
            (criterion_id,),
        ).fetchone()
        commit_hash = crit_row["commit_hash"] if crit_row else None

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

        # Find window start.
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
            # Original single-criterion logic: most recent completed criterion for same task.
            # Order by the effective timestamp so the window boundary matches the recompute
            # path in tusk-call-breakdown --criterion.
            prev = conn.execute(
                "SELECT COALESCE(committed_at, completed_at) AS window_ts "
                "FROM acceptance_criteria "
                "WHERE task_id = ? AND id <> ? AND completed_at IS NOT NULL "
                "ORDER BY COALESCE(committed_at, completed_at) DESC LIMIT 1",
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

        # For a shared-commit group, use the latest completed_at among all group members
        # as the window end so the full group's cost is captured.
        if n > 1:
            latest_row = conn.execute(
                "SELECT MAX(completed_at) AS max_ts FROM acceptance_criteria "
                "WHERE task_id = ? AND commit_hash = ? AND is_completed = 1",
                (task_id, commit_hash),
            ).fetchone()
            window_end = (
                lib.parse_sqlite_timestamp(latest_row["max_ts"])
                if latest_row and latest_row["max_ts"]
                else completed_at
            )
        else:
            window_end = completed_at

        transcript_path = lib.find_transcript()
        if not transcript_path or not os.path.isfile(transcript_path):
            return

        totals = lib.aggregate_session(transcript_path, window_start, window_end)
        if totals["request_count"] == 0:
            return

        tokens_in = lib.compute_tokens_in(totals)
        tokens_out = totals["output_tokens"]
        cost = lib.compute_cost(totals)

        if n > 1:
            # Split cost evenly across all N criteria in the group.
            # Update ALL group members so the final state is consistent regardless
            # of which criterion triggered this call.
            split_cost = cost / n
            split_tokens_in = tokens_in // n
            split_tokens_out = tokens_out // n
            for gid in group_ids:
                conn.execute(
                    "UPDATE acceptance_criteria "
                    "SET cost_dollars = ?, tokens_in = ?, tokens_out = ? "
                    "WHERE id = ?",
                    (split_cost, split_tokens_in, split_tokens_out, gid),
                )
            for gid in group_ids:
                _capture_criterion_tool_stats(conn, gid, task_id, transcript_path, window_start, window_end, n)
        else:
            conn.execute(
                "UPDATE acceptance_criteria "
                "SET cost_dollars = ?, tokens_in = ?, tokens_out = ? "
                "WHERE id = ?",
                (cost, tokens_in, tokens_out, criterion_id),
            )
            # Per-tool breakdown into tool_call_stats
            _capture_criterion_tool_stats(conn, criterion_id, task_id, transcript_path, window_start, window_end)
    except Exception:
        pass  # Best-effort — never block completion


def _capture_criterion_tool_stats(
    conn: sqlite3.Connection,
    criterion_id: int,
    task_id: int,
    transcript_path: Optional[str],
    window_start,
    window_end=None,
    scale: int = 1,
) -> None:
    """Best-effort: aggregate per-tool costs for a criterion and upsert into tool_call_stats.

    scale > 1 divides all cost/token values evenly for shared-commit groups.
    """
    if not transcript_path:
        return
    try:
        stats: dict = {}
        for item in lib.iter_tool_call_costs(transcript_path, window_start, window_end):
            tool = item["tool_name"]
            if tool not in stats:
                stats[tool] = {"call_count": 0, "total_cost": 0.0, "max_cost": 0.0, "tokens_out": 0, "tokens_in": 0}
            s = stats[tool]
            s["call_count"] += 1
            s["total_cost"] += item["cost"]
            s["max_cost"] = max(s["max_cost"], item["cost"])
            s["tokens_out"] += item["output_tokens"]
            s["tokens_in"] += item["marginal_input_tokens"]

        if scale > 1:
            for s in stats.values():
                s["call_count"] = s["call_count"] // scale
                s["total_cost"] /= scale
                s["max_cost"] /= scale
                s["tokens_out"] //= scale
                s["tokens_in"] //= scale

        lib.upsert_criterion_tool_stats(conn, criterion_id, task_id, stats)
    except Exception:
        pass  # Best-effort — never block completion


# ── Verification ──────────────────────────────────────────────────────

# Shell prefix injected before every code/test criterion spec. Redefines `grep`
# (POSIX function, inherited by subshells and pipeline stages) so recursive
# greps skip dirs that grep -r scans but users never mean to match: Python
# bytecode caches, pytest caches, and JS dependency trees. grep -r ignores
# .gitignore, so without this a spec like `! grep -rE "foo" skills/` can fail
# because "foo" appears inside a compiled .pyc file. --exclude-dir is a no-op
# for non-recursive grep, so this is safe for specs that don't use -r/-R.
_GREP_EXCLUDE_PREFIX = (
    'grep() { command grep '
    '--exclude-dir=__pycache__ '
    '--exclude-dir=.pytest_cache '
    '--exclude-dir=node_modules "$@"; }; '
)


_CODE_TIMEOUT_SECS = 120
# Test suites grow; subprocess.run(capture_output=True) can also slow pytest by
# ~2.5x vs direct invocation because stdout/stderr pipes are drained serially.
_TEST_TIMEOUT_SECS = 300


def run_verification(criterion_type: str, spec: str) -> dict:
    """Run automated verification based on criterion type.

    Returns {"passed": bool, "output": str}.
    """
    if criterion_type == "manual" or not spec:
        return {"passed": True, "output": ""}

    if criterion_type in ("code", "test"):
        timeout = _TEST_TIMEOUT_SECS if criterion_type == "test" else _CODE_TIMEOUT_SECS
        t0 = time.monotonic()
        try:
            result = subprocess.run(
                _GREP_EXCLUDE_PREFIX + spec,
                shell=True, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
            )
            elapsed = time.monotonic() - t0
            output = result.stdout.strip()
            if result.stderr.strip():
                output += ("\n" if output else "") + result.stderr.strip()
            passed = result.returncode == 0
            if not passed:
                header = f"exit_code={result.returncode}, elapsed={elapsed:.1f}s\n"
                output = header + output
            # Truncate long output
            if len(output) > 2000:
                output = output[:2000] + "\n... (truncated)"
            return {"passed": passed, "output": output}
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            return {
                "passed": False,
                "output": f"exit_code=timeout, elapsed={elapsed:.1f}s\nVerification timed out ({timeout}s)",
            }
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

def cmd_add(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)
    try:
        # Verify task exists
        task = conn.execute("SELECT id FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        # Validate criterion_type against config
        criterion_types = config.get("criterion_types", [])
        if criterion_types and args.type not in criterion_types:
            joined = ", ".join(criterion_types)
            print(f"Error: Invalid criterion type '{args.type}'. Valid: {joined}", file=sys.stderr)
            return 2

        # Validate spec: required for non-manual types
        if args.type in SPEC_REQUIRED_TYPES and not args.spec:
            print(f"Error: --spec is required for criterion type '{args.type}'", file=sys.stderr)
            return 2

        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, source, criterion_type, verification_spec) "
            "VALUES (?, ?, ?, ?, ?)",
            (args.task_id, args.text, args.source, args.type, args.spec),
        )
        conn.commit()

        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        type_suffix = f" (type: {args.type})" if args.type != "manual" else ""
        print(f"Added criterion #{cid} to task #{args.task_id}{type_suffix}")
        return 0
    finally:
        conn.close()


def cmd_list(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)
    try:
        # Verify task exists
        task = conn.execute(
            "SELECT id, summary FROM tasks WHERE id = ?", (args.task_id,)
        ).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        rows = conn.execute(
            "SELECT id, criterion, source, is_completed, is_deferred, deferred_reason, "
            "cost_dollars, tokens_in, tokens_out, "
            "criterion_type, verification_spec, commit_hash, committed_at, "
            "skip_note, created_at "
            "FROM acceptance_criteria WHERE task_id = ? ORDER BY id",
            (args.task_id,),
        ).fetchall()
    finally:
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
        if r["is_completed"] and r["skip_note"]:
            criterion_text += f" [skip: {r['skip_note']}]"
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


def _done_single(conn: sqlite3.Connection, criterion_id: int, skip_verify: bool,
                  suppress_shared_commit: bool, commit_hash: Optional[str],
                  committed_at: Optional[str], note: Optional[str] = None) -> int:
    """Mark a single criterion as done. Returns 0 on success, 1 on verification failure, 2 on not-found."""
    row = conn.execute(
        "SELECT id, task_id, criterion, is_completed, criterion_type, verification_spec "
        "FROM acceptance_criteria WHERE id = ?",
        (criterion_id,),
    ).fetchone()
    if not row:
        print(f"Error: Criterion {criterion_id} not found", file=sys.stderr)
        return 2

    if row["is_completed"]:
        print(f"Criterion #{criterion_id} is already completed")
        return 0

    criterion_type = row["criterion_type"] or "manual"
    spec = row["verification_spec"]

    # Run verification for non-manual types (unless --skip-verify)
    verification_result = None
    if criterion_type != "manual" and spec and not skip_verify:
        result = run_verification(criterion_type, spec)
        verification_result = json.dumps(result)

        if not result["passed"]:
            # Store the failed result
            conn.execute(
                "UPDATE acceptance_criteria SET verification_result = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (verification_result, criterion_id),
            )
            conn.commit()

            print(f"Verification FAILED for criterion #{criterion_id} ({criterion_type}):",
                  file=sys.stderr)
            if result["output"]:
                print(result["output"], file=sys.stderr)
            print("Use --skip-verify to bypass verification.", file=sys.stderr)
            return 1

    # Warn if another completed criterion on this task already has this commit hash.
    # Suppress when flag is set (batch/allow-shared-commit).
    if commit_hash is not None and not suppress_shared_commit:
        dup = conn.execute(
            "SELECT id FROM acceptance_criteria "
            "WHERE task_id = ? AND id <> ? AND commit_hash = ? AND is_completed = 1 "
            "LIMIT 1",
            (row["task_id"], criterion_id, commit_hash),
        ).fetchone()
        if dup:
            print(
                f"Warning: Criterion #{criterion_id} shares commit {commit_hash} "
                f"with criterion #{dup['id']}.\n"
                f"If these criteria were completed together intentionally (e.g. tightly coupled changes), "
                f"this is fine — pass --allow-shared-commit to suppress this warning.\n"
                f"If this is a big-bang commit covering unrelated changes, consider splitting: "
                f"one commit per criterion improves cost attribution and reviewability.",
                file=sys.stderr,
            )

    conn.execute(
        "UPDATE acceptance_criteria SET is_completed = 1, "
        "completed_at = strftime('%Y-%m-%d %H:%M:%f', 'now'), "
        "commit_hash = ?, committed_at = ?, "
        "verification_result = ?, skip_note = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (commit_hash, committed_at, verification_result, note, criterion_id),
    )
    conn.commit()

    # Best-effort cost capture — pass completed_at to bound the transcript window
    crit_ts = conn.execute(
        "SELECT completed_at FROM acceptance_criteria WHERE id = ?",
        (criterion_id,),
    ).fetchone()
    completed_at_dt = (
        lib.parse_sqlite_timestamp(crit_ts["completed_at"])
        if crit_ts and crit_ts["completed_at"]
        else None
    )
    capture_criterion_cost(conn, criterion_id, row["task_id"], completed_at_dt)
    conn.commit()

    verified_msg = ""
    if criterion_type != "manual" and not skip_verify:
        verified_msg = " (verification passed)"
    elif criterion_type != "manual" and skip_verify:
        verified_msg = " (verification skipped)"
    if sys.stdout.isatty():
        print(f"Criterion #{criterion_id} marked done{verified_msg}: {row['criterion']}")
    return 0


def cmd_done(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)
    try:
        # Best-effort: capture current git HEAD short hash and commit timestamp (once for all)
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

        criterion_ids = args.criterion_ids
        allow_shared = getattr(args, "allow_shared_commit", False)
        batch = getattr(args, "batch", False)
        note = getattr(args, "note", None)

        worst_exit = 0
        for i, cid in enumerate(criterion_ids):
            # For bulk mode (multiple IDs), imply --batch for 2nd+ criterion
            suppress = allow_shared or batch or (i > 0 and len(criterion_ids) > 1)
            rc = _done_single(conn, cid, args.skip_verify, suppress, commit_hash, committed_at, note=note)
            if rc > worst_exit:
                worst_exit = rc
        return worst_exit
    finally:
        conn.close()


def cmd_skip(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT id, task_id, criterion, is_completed, is_deferred, deferred_reason "
            "FROM acceptance_criteria WHERE id = ?",
            (args.criterion_id,),
        ).fetchone()
        if not row:
            print(f"Error: Criterion {args.criterion_id} not found", file=sys.stderr)
            return 2

        if row["is_completed"]:
            print(f"Criterion #{args.criterion_id} is already completed")
            return 0

        if row["is_deferred"]:
            print(
                f"Criterion #{args.criterion_id} is already deferred "
                f"(reason: {row['deferred_reason']}): {row['criterion']}"
            )
            return 0

        conn.execute(
            "UPDATE acceptance_criteria SET is_deferred = 1, deferred_reason = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (args.reason, args.criterion_id),
        )
        conn.commit()
        print(f"Criterion #{args.criterion_id} marked as deferred (reason: {args.reason}): {row['criterion']}")
        return 0
    finally:
        conn.close()


def cmd_reset(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT id, task_id, criterion, is_completed, is_deferred "
            "FROM acceptance_criteria WHERE id = ?",
            (args.criterion_id,),
        ).fetchone()
        if not row:
            print(f"Error: Criterion {args.criterion_id} not found", file=sys.stderr)
            return 2

        if not row["is_completed"] and not row["is_deferred"]:
            print(f"Criterion #{args.criterion_id} is already incomplete and not deferred")
            return 0

        conn.execute(
            "UPDATE acceptance_criteria SET is_completed = 0, completed_at = NULL, "
            "cost_dollars = NULL, tokens_in = NULL, tokens_out = NULL, "
            "verification_result = NULL, commit_hash = NULL, committed_at = NULL, "
            "is_deferred = 0, deferred_reason = NULL, skip_note = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (args.criterion_id,),
        )
        conn.commit()
        print(f"Criterion #{args.criterion_id} reset to incomplete: {row['criterion']}")
        return 0
    finally:
        conn.close()


def cmd_finish_deferred(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)
    try:
        placeholders = ", ".join("?" for _ in args.task_ids)
        rows = conn.execute(
            f"SELECT id FROM acceptance_criteria "
            f"WHERE is_deferred = 1 AND deferred_reason = ? AND is_completed = 0 "
            f"AND task_id IN ({placeholders})",
            [args.reason] + args.task_ids,
        ).fetchall()
        if not rows:
            print(json.dumps({"marked": 0}))
            return 0
        ids = [r["id"] for r in rows]
        id_placeholders = ", ".join("?" for _ in ids)
        conn.execute(
            f"UPDATE acceptance_criteria SET is_completed = 1, "
            f"completed_at = datetime('now'), updated_at = datetime('now') "
            f"WHERE id IN ({id_placeholders})",
            ids,
        )
        conn.commit()
        print(json.dumps({"marked": len(ids)}))
        return 0
    finally:
        conn.close()


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print("Usage: tusk criteria {add|list|done|skip|reset|finish-deferred} ...", file=sys.stderr)
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
    done_p = subparsers.add_parser("done", help="Mark one or more criteria as completed")
    done_p.add_argument("criterion_ids", type=int, nargs="+", help="One or more criterion IDs")
    done_p.add_argument(
        "--skip-verify", action="store_true",
        help="Skip automated verification for non-manual criteria",
    )
    done_p.add_argument(
        "--allow-shared-commit", action="store_true",
        help="Suppress the shared-commit warning (use when intentionally marking multiple criteria on the same commit)",
    )
    done_p.add_argument(
        "--batch", action="store_true",
        help="Suppress shared-commit warning for criteria after the first in a multi-criteria tusk commit invocation",
    )
    done_p.add_argument(
        "--note",
        help="Rationale to attach to the criterion (requires --skip-verify)",
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

    # finish-deferred
    fd_p = subparsers.add_parser(
        "finish-deferred",
        help="Mark all deferred criteria with a given reason as completed for specified tasks",
    )
    fd_p.add_argument("--reason", required=True, help="Deferred reason to match (e.g., 'chain')")
    fd_p.add_argument("task_ids", type=int, nargs="+", help="One or more task IDs")

    args = parser.parse_args(sys.argv[3:])

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "done" and getattr(args, "note", None) and not args.skip_verify:
        done_p.error("--note requires --skip-verify")

    try:
        handlers = {
            "add": cmd_add, "list": cmd_list, "done": cmd_done,
            "skip": cmd_skip, "reset": cmd_reset,
            "finish-deferred": cmd_finish_deferred,
        }
        sys.exit(handlers[args.command](args, db_path, config))
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
