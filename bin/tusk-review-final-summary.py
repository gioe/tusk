#!/usr/bin/env python3
"""Render the /review-commits Step 10 final summary block for a review.

Given a review_id, look up the associated task and aggregate comment counts
across all of that task's reviews (including superseded passes) to produce:

    Review complete for Task <task_id>: <task_summary>
    ══════════════════════════════════════════════════
    Pass:      <pass number of this review>

    must_fix:  <found> found, <fixed> fixed
    suggest:   <found> found, <fixed> fixed, <dismissed> dismissed
    defer:     <found> found, <created> tasks created, <skipped> skipped (duplicate)

    Verdict: <APPROVED | CHANGES REMAINING>

The verdict matches `tusk review verdict`: APPROVED when no open must_fix
comments remain on non-superseded reviews, CHANGES_REMAINING otherwise. The
machine verdict is mapped to the display label ("CHANGES REMAINING" with a
space) before printing. Deferred-task creation is distinguished from
skipped-duplicates via `review_comments.deferred_task_id` — populated means a
task was created, NULL means the deferred finding was skipped (dupe).

Usage:
    tusk review-final-summary <review_id>

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (unused)
    sys.argv[3] — review_id
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

_db_lib = tusk_loader.load("tusk-db-lib")
get_connection = _db_lib.get_connection


DIVIDER = "═" * 50


def _counts_for_task(conn: sqlite3.Connection, task_id: int) -> dict:
    """Aggregate comment counts across all of a task's reviews (all passes).

    Superseded reviews are included so multi-pass totals reflect the full
    review journey, not just the last pass.
    """
    rows = conn.execute(
        "SELECT rc.category, rc.resolution, rc.deferred_task_id"
        " FROM review_comments rc"
        " JOIN code_reviews cr ON cr.id = rc.review_id"
        " WHERE cr.task_id = ?",
        (task_id,),
    ).fetchall()

    counts = {
        "must_fix": {"found": 0, "fixed": 0},
        "suggest": {"found": 0, "fixed": 0, "dismissed": 0},
        "defer": {"found": 0, "created": 0, "skipped": 0},
    }
    for r in rows:
        cat = r["category"]
        res = r["resolution"]
        if cat == "must_fix":
            counts["must_fix"]["found"] += 1
            if res == "fixed":
                counts["must_fix"]["fixed"] += 1
        elif cat == "suggest":
            counts["suggest"]["found"] += 1
            if res == "fixed":
                counts["suggest"]["fixed"] += 1
            elif res == "dismissed":
                counts["suggest"]["dismissed"] += 1
        elif cat == "defer":
            counts["defer"]["found"] += 1
            if res == "deferred":
                if r["deferred_task_id"] is not None:
                    counts["defer"]["created"] += 1
                else:
                    counts["defer"]["skipped"] += 1
    return counts


def _open_must_fix(conn: sqlite3.Connection, task_id: int) -> int:
    """Count unresolved must_fix comments on non-superseded reviews.

    Mirrors cmd_verdict in tusk-review.py so the verdict printed here stays in
    lockstep with `tusk review verdict`.
    """
    row = conn.execute(
        "SELECT COUNT(*) as cnt"
        " FROM review_comments rc"
        " JOIN code_reviews cr ON cr.id = rc.review_id"
        " WHERE cr.task_id = ? AND cr.status <> 'superseded'"
        " AND rc.category = 'must_fix' AND rc.resolution IS NULL",
        (task_id,),
    ).fetchone()
    return row["cnt"] if row else 0


def render_summary(review_id: int, db_path: str) -> int:
    conn = get_connection(db_path)
    try:
        review = conn.execute(
            "SELECT r.id, r.task_id, r.review_pass, t.summary as task_summary"
            " FROM code_reviews r JOIN tasks t ON t.id = r.task_id"
            " WHERE r.id = ?",
            (review_id,),
        ).fetchone()
        if not review:
            print(f"Error: Review {review_id} not found", file=sys.stderr)
            return 2

        task_id = review["task_id"]
        task_summary = review["task_summary"]
        pass_num = review["review_pass"]

        counts = _counts_for_task(conn, task_id)
        open_must_fix = _open_must_fix(conn, task_id)
    finally:
        conn.close()

    verdict_label = "APPROVED" if open_must_fix == 0 else "CHANGES REMAINING"

    mf = counts["must_fix"]
    sg = counts["suggest"]
    df = counts["defer"]

    print(f"Review complete for Task {task_id}: {task_summary}")
    print(DIVIDER)
    print(f"Pass:      {pass_num}")
    print()
    print(f"must_fix:  {mf['found']} found, {mf['fixed']} fixed")
    print(f"suggest:   {sg['found']} found, {sg['fixed']} fixed, {sg['dismissed']} dismissed")
    print(f"defer:     {df['found']} found, {df['created']} tasks created, {df['skipped']} skipped (duplicate)")
    print()
    print(f"Verdict: {verdict_label}")
    return 0


def main():
    if len(sys.argv) < 4:
        print("Usage: tusk review-final-summary <review_id>", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    # sys.argv[2] is config_path, unused

    parser = argparse.ArgumentParser(
        prog="tusk review-final-summary",
        description="Render the /review-commits Step 10 final summary block for a review",
    )
    parser.add_argument("review_id", type=int, help="Review ID")
    args = parser.parse_args(sys.argv[3:])

    try:
        sys.exit(render_summary(args.review_id, db_path))
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
