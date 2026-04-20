#!/usr/bin/env python3
"""Block until a code review reaches a terminal state, or time out.

Collapses the /review-commits Step 6 monitoring loop (``sleep 30`` in bash,
then ``tusk review status`` poll, repeat up to ~5 iterations) into a single
subcommand. The orchestrator calls this once and either (a) proceeds with a
verdict, or (b) gets a ``timed_out: true`` JSON payload and handles the
stall/no-verdict paths via ``TaskOutput`` on the spawned reviewer agent.

Moving the poll here matters because the Claude Code runtime blocks long
``sleep`` commands at the orchestrator level; encoding the wait as a CLI
subcommand means the ``time.sleep`` runs inside this subprocess, which the
runtime does not police.

Usage:
    tusk review-wait <review_id> [--interval 30] [--timeout-seconds 150]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (unused)
    sys.argv[3:] — review_id plus optional flags

Output JSON (stdout, exit 0):
    {
        "review_id": <int>,
        "task_id": <int>,
        "status": "approved" | "changes_requested" | "superseded" | "pending",
        "review_pass": <int> | null,
        "reviewer": <str> | null,
        "timed_out": <bool>,
        "elapsed_seconds": <float>,
        "polls": <int>
    }

``timed_out`` is ``true`` iff we exited because the wall clock hit the
timeout while status was still pending/in_progress. Any terminal status
(approved, changes_requested, superseded) returns ``timed_out: false`` even
if the timeout would have fired on the next poll.

Exit codes:
    0 — review reached a terminal state OR the timeout was reached (both are
        expected outcomes; the caller branches on ``timed_out``)
    1 — bad arguments, or review_id not found in code_reviews
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

_db_lib = tusk_loader.load("tusk-db-lib")
get_connection = _db_lib.get_connection

TERMINAL_STATUSES = ("approved", "changes_requested", "superseded")

DEFAULT_INTERVAL_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 150  # 5 polls at 30s, matching legacy STALL_THRESHOLD


def _fetch_review(db_path: str, review_id: int) -> dict | None:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT id, task_id, reviewer, status, review_pass"
            " FROM code_reviews WHERE id = ?",
            (review_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "review_id": row["id"],
        "task_id": row["task_id"],
        "reviewer": row["reviewer"],
        "status": row["status"],
        "review_pass": row["review_pass"],
    }


def wait_for_terminal(
    db_path: str,
    review_id: int,
    interval_seconds: float,
    timeout_seconds: float,
    *,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> dict:
    """Poll code_reviews.status until terminal or timeout. Return a payload dict."""
    start = monotonic_fn()
    polls = 0

    while True:
        review = _fetch_review(db_path, review_id)
        polls += 1
        if review is None:
            raise SystemExit(f"Review #{review_id} not found in code_reviews.")

        elapsed = monotonic_fn() - start

        if review["status"] in TERMINAL_STATUSES:
            return {
                **review,
                "timed_out": False,
                "elapsed_seconds": round(elapsed, 3),
                "polls": polls,
            }

        if elapsed >= timeout_seconds:
            return {
                **review,
                "timed_out": True,
                "elapsed_seconds": round(elapsed, 3),
                "polls": polls,
            }

        # Don't oversleep past the deadline: cap the next sleep so total
        # elapsed never exceeds timeout_seconds by more than one poll window.
        remaining = max(0.0, timeout_seconds - elapsed)
        sleep_fn(min(interval_seconds, remaining))


def main(argv: list) -> int:
    if len(argv) < 3:
        print(
            "Usage: tusk review-wait <review_id> [--interval SECONDS] [--timeout-seconds SECONDS]",
            file=sys.stderr,
        )
        return 1

    db_path = argv[0]
    # argv[1] is config_path — reserved for future use

    parser = argparse.ArgumentParser(
        prog="tusk review-wait",
        description="Block until a code review reaches a terminal state or times out.",
    )
    parser.add_argument("review_id", type=int, help="code_reviews.id to poll")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"seconds between polls (default {DEFAULT_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"total wall-clock seconds before giving up (default {DEFAULT_TIMEOUT_SECONDS})",
    )
    args = parser.parse_args(argv[2:])

    if args.interval <= 0:
        print("--interval must be > 0", file=sys.stderr)
        return 1
    if args.timeout_seconds <= 0:
        print("--timeout-seconds must be > 0", file=sys.stderr)
        return 1

    try:
        payload = wait_for_terminal(
            db_path,
            args.review_id,
            args.interval,
            args.timeout_seconds,
        )
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 1

    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
