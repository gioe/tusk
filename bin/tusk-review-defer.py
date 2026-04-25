#!/usr/bin/env python3
"""Defer a review comment: dupe-check, insert deferred task, resolve comment.

Collapses the three-call dance in /review-commits Step 7 for each defer
comment into a single subcommand:

    1. Read the comment text from the review_comments row by <comment_id>.
    2. Run ``tusk dupes check`` against the comment summary (first line) in
       the given --domain. Three outcomes:
         - exit 0 (no duplicate)  -> insert a deferred task
         - exit 1 (duplicate)     -> skip insert, record matched_task_id
         - other (check failed)   -> skip insert, record dupe_check_failed
    3. Mark the comment resolved as 'deferred' in all three branches so the
       review verdict can progress regardless of whether a new task was
       actually created.

Usage:
    tusk review-defer <comment_id> --domain <d> --task-type <t>

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (unused; task-insert validates against it)
    sys.argv[3:] — comment_id, --domain, --task-type

Output JSON (stdout on success, including skip branches):
    {
        "created_task_id": <int> | null,
        "skipped_reason": "duplicate" | "dupe_check_failed" | null,
        "matched_task_id": <int> | null
    }

Exit codes:
    0 — comment was resolved (task created, duplicate matched, or check failed-but-resolved)
    1 — bad arguments, comment not found, or the resolve call itself failed
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps

_TUSK_WRAPPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk")


def read_comment(db_path: str, comment_id: int) -> tuple[str, str]:
    """Return (summary, body) for the review_comments row with this id.

    summary = first non-empty line of the comment text, trimmed.
    body = full comment text as stored.
    """
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT comment FROM review_comments WHERE id = ?",
            (comment_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SystemExit(f"Comment #{comment_id} not found in review_comments.")

    body = row[0] or ""
    summary = ""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            summary = stripped
            break
    if not summary:
        summary = body.strip()
    return summary, body


def run_dupe_check(summary: str, domain: str) -> tuple[int, int | None]:
    """Return (exit_code, matched_task_id).

    matched_task_id is the highest-similarity task id when exit_code == 1,
    otherwise None.
    """
    cmd = [_TUSK_WRAPPER, "dupes", "check", summary, "--json", "--domain", domain]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    matched_task_id: int | None = None
    if r.returncode == 1:
        try:
            data = json.loads(r.stdout or "{}")
            dupes = data.get("duplicates", []) or []
            if dupes:
                raw_id = dupes[0].get("id")
                if isinstance(raw_id, int):
                    matched_task_id = raw_id
                else:
                    try:
                        matched_task_id = int(raw_id)
                    except (TypeError, ValueError):
                        matched_task_id = None
        except json.JSONDecodeError:
            matched_task_id = None
    return r.returncode, matched_task_id


def run_task_insert(summary: str, body: str, domain: str, task_type: str) -> int:
    """Insert a deferred task; return the new task_id."""
    criterion = f"Address deferred finding: {summary}"
    cmd = [
        _TUSK_WRAPPER, "task-insert",
        summary, body,
        "--priority", "Medium",
        "--domain", domain,
        "--task-type", task_type,
        "--criteria", criterion,
        "--deferred",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip() or "tusk task-insert failed"
        raise SystemExit(f"tusk task-insert failed (exit {r.returncode}): {msg}")
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"tusk task-insert returned non-JSON stdout: {r.stdout!r} ({exc})"
        )
    task_id = data.get("task_id")
    if not isinstance(task_id, int):
        raise SystemExit(
            f"tusk task-insert JSON missing integer task_id: {data!r}"
        )
    return task_id


def run_review_resolve(comment_id: int, deferred_task_id: int | None = None) -> None:
    """Mark the comment resolved as 'deferred'.

    When ``deferred_task_id`` is provided, threads it through to
    ``tusk review resolve --deferred-task-id`` so review_comments.deferred_task_id
    records the task that was actually created. Without this link,
    tusk review-final-summary counts the deferred finding as skipped-duplicate.
    """
    cmd = [_TUSK_WRAPPER, "review", "resolve", str(comment_id), "deferred"]
    if deferred_task_id is not None:
        cmd.extend(["--deferred-task-id", str(deferred_task_id)])
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip() or "tusk review resolve failed"
        raise SystemExit(
            f"tusk review resolve {comment_id} deferred failed "
            f"(exit {r.returncode}): {msg}"
        )


def defer_comment(db_path: str, comment_id: int, domain: str, task_type: str) -> dict:
    """Full defer flow. Always resolves the comment; returns the result shape."""
    summary, body = read_comment(db_path, comment_id)

    exit_code, matched_task_id = run_dupe_check(summary, domain)

    created_task_id: int | None = None
    skipped_reason: str | None = None

    if exit_code == 0:
        created_task_id = run_task_insert(summary, body, domain, task_type)
    elif exit_code == 1:
        skipped_reason = "duplicate"
    else:
        skipped_reason = "dupe_check_failed"

    run_review_resolve(comment_id, deferred_task_id=created_task_id)

    return {
        "created_task_id": created_task_id,
        "skipped_reason": skipped_reason,
        "matched_task_id": matched_task_id,
    }


def main(argv: list) -> int:
    if len(argv) < 3:
        print(
            "Usage: tusk review-defer <comment_id> --domain <d> --task-type <t>",
            file=sys.stderr,
        )
        return 1

    db_path = argv[0]
    # argv[1] is config_path — validation happens inside task-insert

    parser = argparse.ArgumentParser(
        prog="tusk review-defer",
        description="Defer a review comment: dupe-check, insert deferred task, resolve comment",
    )
    parser.add_argument("comment_id", help="review_comments.id to defer")
    parser.add_argument("--domain", required=True, help="Domain for the deferred task (and dupe scope)")
    parser.add_argument(
        "--task-type", dest="task_type", required=True,
        help="Task type for the deferred task (e.g. 'bug')",
    )
    args = parser.parse_args(argv[2:])

    try:
        comment_id = int(re.sub(r"^#", "", str(args.comment_id)))
    except ValueError:
        print(f"Invalid comment_id: {args.comment_id}", file=sys.stderr)
        return 1

    try:
        result = defer_comment(db_path, comment_id, args.domain, args.task_type)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
        return 1

    print(dumps(result))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk review-defer <comment_id> --domain <d> --task-type <t>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
