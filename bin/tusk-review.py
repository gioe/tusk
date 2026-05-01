#!/usr/bin/env python3
"""Manage code reviews for tusk tasks.

Called by the tusk wrapper:
    tusk review start|begin|add-comment|list|resolve|approve|request-changes|backfill-cost|status|summary ...

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — subcommand + flags
"""

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py, tusk-json-lib.py, tusk-review-diff-range.py, tusk-pricing-lib.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection

_pricing_lib = None  # populated lazily by _load_pricing_lib()


def _load_pricing_lib():
    """Load the pricing library on first use.

    Lazy so test paths that never touch cost capture don't pay for the
    import (and so we can monkeypatch this hook in tests to inject a stub).
    """
    global _pricing_lib
    if _pricing_lib is None:
        _pricing_lib = tusk_loader.load("tusk-pricing-lib")
        _pricing_lib.load_pricing()
    return _pricing_lib


def _compute_review_cost_from_window(created_at: str) -> dict | None:
    """Aggregate the transcript between *created_at* and now; return cost+tokens.

    Mirrors the cost computation in `tusk skill-run finish` but uses the
    review row's `created_at` as the window start. Returns None when no
    transcript is discoverable or the window contains zero requests —
    callers leave the columns NULL in that case rather than writing zeros,
    so a missing transcript stays distinguishable from a real $0 review.
    """
    lib = _load_pricing_lib()
    started_at = lib.parse_sqlite_timestamp(created_at)
    transcript_path = lib.find_transcript()
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    totals = lib.aggregate_session(transcript_path, started_at, None)
    if totals.get("request_count", 0) == 0:
        return None
    return {
        "cost_dollars": lib.compute_cost(totals),
        "tokens_in": lib.compute_tokens_in(totals),
        "tokens_out": totals["output_tokens"],
        "model": totals.get("model"),
    }


def _add_cost_flags(parser: argparse.ArgumentParser) -> None:
    """Wire the cost-capture flags onto an approve/request-changes subparser."""
    parser.add_argument(
        "--cost-dollars",
        dest="cost_dollars",
        type=float,
        default=None,
        help="Override the auto-computed cost (USD) for this review row.",
    )
    parser.add_argument(
        "--tokens-in",
        dest="tokens_in",
        type=int,
        default=None,
        help="Override the auto-computed tokens_in count for this review row.",
    )
    parser.add_argument(
        "--tokens-out",
        dest="tokens_out",
        type=int,
        default=None,
        help="Override the auto-computed tokens_out count for this review row.",
    )
    parser.add_argument(
        "--skip-cost",
        dest="skip_cost",
        action="store_true",
        help="Skip transcript-based cost auto-compute on this call.",
    )


def _resolve_cost_columns(args, created_at: str) -> tuple:
    """Return (cost_dollars, tokens_in, tokens_out) to set on the review row.

    Per-column priority: explicit --cost-dollars/--tokens-in/--tokens-out
    flags override; otherwise auto-compute from the transcript window. A
    None in any slot means "leave the column alone." `--skip-cost` short-
    circuits the auto-compute so callers (bakeoffs, manual fixes) can
    disable it without passing all three explicit values.
    """
    explicit_cost = getattr(args, "cost_dollars", None)
    explicit_tin = getattr(args, "tokens_in", None)
    explicit_tout = getattr(args, "tokens_out", None)
    skip_cost = getattr(args, "skip_cost", False)

    if skip_cost or all(x is not None for x in (explicit_cost, explicit_tin, explicit_tout)):
        return explicit_cost, explicit_tin, explicit_tout

    computed = _compute_review_cost_from_window(created_at)
    if computed is None:
        return explicit_cost, explicit_tin, explicit_tout

    return (
        explicit_cost if explicit_cost is not None else computed["cost_dollars"],
        explicit_tin if explicit_tin is not None else computed["tokens_in"],
        explicit_tout if explicit_tout is not None else computed["tokens_out"],
    )


def load_review_config(config_path: str) -> dict:
    """Load review-related config values."""
    try:
        with open(config_path) as f:
            config = json.load(f)
        return {
            "reviewer": config.get("review", {}).get("reviewer"),
            "max_passes": config.get("review", {}).get("max_passes", 2),
            "categories": config.get("review_categories", []),
            "severities": config.get("review_severities", []),
        }
    except (OSError, json.JSONDecodeError):
        return {"reviewer": None, "max_passes": 2, "categories": [], "severities": []}


def cmd_start(args: argparse.Namespace, db_path: str, config_path: str) -> int:
    """Create one code_reviews row for the configured reviewer (or unassigned)."""
    if args.diff_summary is not None and not args.diff_summary.strip():
        print(
            "Error: --diff-summary must not be empty or whitespace-only. "
            "Either omit the flag or pass a non-empty summary.",
            file=sys.stderr,
        )
        return 1

    conn = get_connection(db_path)
    try:
        task = conn.execute("SELECT id, summary FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        # Supersede any existing pending reviews from prior passes before creating new ones
        prior_pending = conn.execute(
            "SELECT id FROM code_reviews WHERE task_id = ? AND status = 'pending'",
            (args.task_id,),
        ).fetchall()
        if prior_pending:
            conn.execute(
                "UPDATE code_reviews SET status = 'superseded', updated_at = datetime('now')"
                " WHERE task_id = ? AND status = 'pending'",
                (args.task_id,),
            )
            conn.commit()
            superseded_ids = ", ".join(f"#{r['id']}" for r in prior_pending)
            print(f"Superseded {len(prior_pending)} prior pending review(s): {superseded_ids}")

        cfg = load_review_config(config_path)
        reviewer_item = cfg["reviewer"]

        # CLI override wins over config
        if args.reviewer:
            reviewer_name = args.reviewer
        elif isinstance(reviewer_item, dict):
            reviewer_name = reviewer_item.get("name")
        elif isinstance(reviewer_item, str):
            reviewer_name = reviewer_item
        else:
            reviewer_name = None

        conn.execute(
            "INSERT INTO code_reviews (task_id, reviewer, status, review_pass, diff_summary, agent_name)"
            " VALUES (?, ?, 'pending', ?, ?, ?)",
            (args.task_id, reviewer_name, args.pass_num, args.diff_summary, args.agent),
        )
        conn.commit()
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()

    reviewer_str = f" (reviewer: {reviewer_name})" if reviewer_name else ""
    print(f"Started review #{rid} for task #{args.task_id}{reviewer_str}: {task['summary']}")

    return 0


def cmd_begin(args: argparse.Namespace, db_path: str, config_path: str) -> int:
    """Bundle review-diff-range and review start into one call.

    Computes the diff range for the task, then creates a code_reviews row with
    the captured summary baked in. Returns combined JSON on stdout so callers
    never have to extract the diff summary from JSON in shell — the field most
    likely to break the `echo "$VAR" | jq` quoting hazard is no longer in the
    output. Output keys: review_id, task_id, reviewer, range, diff_lines,
    recovered_from_task_commits.
    """
    diff_range_mod = tusk_loader.load("tusk-review-diff-range")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))

    try:
        diff_payload = diff_range_mod.compute_range(args.task_id, repo_root)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
        return 1

    diff_summary = diff_payload["summary"]

    conn = get_connection(db_path)
    try:
        task = conn.execute(
            "SELECT id, summary FROM tasks WHERE id = ?", (args.task_id,)
        ).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        conn.execute(
            "UPDATE code_reviews SET status = 'superseded', updated_at = datetime('now')"
            " WHERE task_id = ? AND status = 'pending'",
            (args.task_id,),
        )
        conn.commit()

        cfg = load_review_config(config_path)
        reviewer_item = cfg["reviewer"]
        if args.reviewer:
            reviewer_name = args.reviewer
        elif isinstance(reviewer_item, dict):
            reviewer_name = reviewer_item.get("name")
        elif isinstance(reviewer_item, str):
            reviewer_name = reviewer_item
        else:
            reviewer_name = None

        conn.execute(
            "INSERT INTO code_reviews (task_id, reviewer, status, review_pass, diff_summary, agent_name)"
            " VALUES (?, ?, 'pending', ?, ?, ?)",
            (args.task_id, reviewer_name, args.pass_num, diff_summary, args.agent),
        )
        conn.commit()
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()

    print(dumps({
        "review_id": rid,
        "task_id": args.task_id,
        "reviewer": reviewer_name,
        "range": diff_payload["range"],
        "diff_lines": diff_payload["diff_lines"],
        "recovered_from_task_commits": diff_payload["recovered_from_task_commits"],
    }))
    return 0


def cmd_add_comment(args: argparse.Namespace, db_path: str, config_path: str) -> int:
    """Insert a review_comments row."""
    conn = get_connection(db_path)
    try:
        review = conn.execute(
            "SELECT id, task_id, reviewer FROM code_reviews WHERE id = ?", (args.review_id,)
        ).fetchone()
        if not review:
            print(f"Error: Review {args.review_id} not found", file=sys.stderr)
            return 2

        cfg = load_review_config(config_path)

        if args.category:
            valid_cats = cfg["categories"]
            if valid_cats and args.category not in valid_cats:
                print(
                    f"Error: Invalid category '{args.category}'. Valid: {', '.join(valid_cats)}",
                    file=sys.stderr,
                )
                return 2

        if args.severity:
            valid_sevs = cfg["severities"]
            if valid_sevs and args.severity not in valid_sevs:
                print(
                    f"Error: Invalid severity '{args.severity}'. Valid: {', '.join(valid_sevs)}",
                    file=sys.stderr,
                )
                return 2

        conn.execute(
            "INSERT INTO review_comments"
            " (review_id, file_path, line_start, line_end, category, severity, comment, resolution)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                args.review_id,
                args.file,
                args.line_start,
                args.line_end,
                args.category,
                args.severity,
                args.comment,
            ),
        )
        conn.commit()

        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()

    loc = ""
    if args.file:
        loc = f" in {args.file}"
        if args.line_start:
            loc += f":{args.line_start}"
    cat_sev = ""
    if args.category or args.severity:
        parts = [x for x in [args.category, args.severity] if x]
        cat_sev = f" [{'/'.join(parts)}]"

    print(f"Added comment #{cid} to review #{args.review_id}{loc}{cat_sev}: {args.comment[:60]}")
    return 0


def cmd_list(args: argparse.Namespace, db_path: str) -> int:
    """Show reviews for a task, grouped by reviewer and category."""
    conn = get_connection(db_path)
    try:
        task = conn.execute(
            "SELECT id, summary FROM tasks WHERE id = ?", (args.task_id,)
        ).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        reviews = conn.execute(
            "SELECT id, reviewer, status, review_pass, created_at"
            " FROM code_reviews WHERE task_id = ? AND status <> 'superseded' ORDER BY id",
            (args.task_id,),
        ).fetchall()

        # Fetch all comments in a single pass to avoid re-opening the connection
        review_ids = [rev["id"] for rev in reviews]
        all_comments: dict[int, list] = {rid: [] for rid in review_ids}
        if review_ids:
            placeholders = ",".join("?" * len(review_ids))
            for c in conn.execute(
                f"SELECT id, review_id, file_path, line_start, category, severity, comment, resolution"
                f" FROM review_comments WHERE review_id IN ({placeholders}) ORDER BY review_id, category, id",
                review_ids,
            ).fetchall():
                all_comments[c["review_id"]].append(c)
    finally:
        conn.close()

    if not reviews:
        print(f"No reviews for task #{args.task_id}: {task['summary']}")
        return 0

    print(f"Reviews for task #{args.task_id}: {task['summary']}")
    print()

    for rev in reviews:
        reviewer_label = rev["reviewer"] or "(unassigned)"
        print(f"  Review #{rev['id']} — {reviewer_label} | status: {rev['status']} | pass {rev['review_pass']} | {rev['created_at']}")

        comments = all_comments.get(rev["id"], [])

        if not comments:
            print("    (no comments)")
            continue

        current_cat = None
        for c in comments:
            cat = c["category"] or "general"
            if cat != current_cat:
                print(f"\n    [{cat.upper()}]")
                current_cat = cat
            loc = ""
            if c["file_path"]:
                loc = f" {c['file_path']}"
                if c["line_start"]:
                    loc += f":{c['line_start']}"
            sev = f"[{c['severity']}] " if c["severity"] else ""
            res = f" ({c['resolution']})" if c["resolution"] is not None else ""
            print(f"    #{c['id']}{loc}: {sev}{c['comment']}{res}")

        print()

    return 0


def cmd_resolve(args: argparse.Namespace, db_path: str) -> int:
    """Update a comment's resolution field."""
    valid_resolutions = ("fixed", "deferred", "dismissed")
    if args.resolution not in valid_resolutions:
        print(
            f"Error: Invalid resolution '{args.resolution}'. Valid: {', '.join(valid_resolutions)}",
            file=sys.stderr,
        )
        return 2

    deferred_task_id = getattr(args, "deferred_task_id", None)
    if deferred_task_id is not None and args.resolution != "deferred":
        print(
            "Error: --deferred-task-id is only valid with resolution='deferred'",
            file=sys.stderr,
        )
        return 2

    conn = get_connection(db_path)
    try:
        comment = conn.execute(
            "SELECT id, comment, resolution FROM review_comments WHERE id = ?",
            (args.comment_id,),
        ).fetchone()
        if not comment:
            print(f"Error: Comment {args.comment_id} not found", file=sys.stderr)
            return 2

        if deferred_task_id is not None:
            conn.execute(
                "UPDATE review_comments SET resolution = ?, deferred_task_id = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (args.resolution, deferred_task_id, args.comment_id),
            )
        else:
            conn.execute(
                "UPDATE review_comments SET resolution = ?, updated_at = datetime('now') WHERE id = ?",
                (args.resolution, args.comment_id),
            )
        conn.commit()
    finally:
        conn.close()

    suffix = f" (deferred_task_id={deferred_task_id})" if deferred_task_id is not None else ""
    print(f"Comment #{args.comment_id} marked '{args.resolution}'{suffix}: {comment['comment'][:60]}")
    return 0


def cmd_approve(args: argparse.Namespace, db_path: str) -> int:
    """Set code_reviews.status = 'approved' and review_pass = 1."""
    conn = get_connection(db_path)
    try:
        review = conn.execute(
            "SELECT id, task_id, reviewer, status, created_at FROM code_reviews WHERE id = ?",
            (args.review_id,),
        ).fetchone()
        if not review:
            print(f"Error: Review {args.review_id} not found", file=sys.stderr)
            return 2

        cost_dollars, tokens_in, tokens_out = _resolve_cost_columns(args, review["created_at"])

        set_clauses = ["status = 'approved'", "review_pass = 1", "updated_at = datetime('now')"]
        params: list = []
        if args.note is not None:
            set_clauses.append("note = ?")
            params.append(args.note or None)
        if args.model is not None:
            set_clauses.append("model = ?")
            params.append(args.model or None)
        if cost_dollars is not None:
            set_clauses.append("cost_dollars = ?")
            params.append(cost_dollars)
        if tokens_in is not None:
            set_clauses.append("tokens_in = ?")
            params.append(tokens_in)
        if tokens_out is not None:
            set_clauses.append("tokens_out = ?")
            params.append(tokens_out)
        params.append(args.review_id)
        conn.execute(
            f"UPDATE code_reviews SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()

    reviewer_str = f" by {review['reviewer']}" if review["reviewer"] else ""
    if args.note == "":
        note_str = " (note cleared)"
    elif args.note:
        note_str = f" ({args.note})"
    else:
        note_str = ""
    print(f"Review #{args.review_id} approved{reviewer_str} for task #{review['task_id']}{note_str}")
    return 0


def cmd_request_changes(args: argparse.Namespace, db_path: str) -> int:
    """Set code_reviews.status = 'changes_requested' and review_pass = 0."""
    conn = get_connection(db_path)
    try:
        review = conn.execute(
            "SELECT id, task_id, reviewer, status, created_at FROM code_reviews WHERE id = ?",
            (args.review_id,),
        ).fetchone()
        if not review:
            print(f"Error: Review {args.review_id} not found", file=sys.stderr)
            return 2

        cost_dollars, tokens_in, tokens_out = _resolve_cost_columns(args, review["created_at"])

        set_clauses = ["status = 'changes_requested'", "review_pass = 0", "updated_at = datetime('now')"]
        params: list = []
        if args.note is not None:
            set_clauses.append("note = ?")
            params.append(args.note or None)
        if args.model is not None:
            set_clauses.append("model = ?")
            params.append(args.model or None)
        if cost_dollars is not None:
            set_clauses.append("cost_dollars = ?")
            params.append(cost_dollars)
        if tokens_in is not None:
            set_clauses.append("tokens_in = ?")
            params.append(tokens_in)
        if tokens_out is not None:
            set_clauses.append("tokens_out = ?")
            params.append(tokens_out)
        params.append(args.review_id)
        conn.execute(
            f"UPDATE code_reviews SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()

    reviewer_str = f" by {review['reviewer']}" if review["reviewer"] else ""
    if args.note == "":
        note_str = " (note cleared)"
    elif args.note:
        note_str = f" ({args.note})"
    else:
        note_str = ""
    print(f"Review #{args.review_id} changes requested{reviewer_str} for task #{review['task_id']}{note_str}")
    return 0


def cmd_backfill_cost(args: argparse.Namespace, db_path: str) -> int:
    """Recompute cost/tokens columns for an existing review row.

    Used to repair rows that finalized without cost data — e.g. rows
    written under an older code path or a `--skip-cost` call. Best-effort:
    if no transcript with requests is discoverable for the row's
    `[created_at, now]` window (transcript rotated, ran on another host),
    leaves the row unchanged and returns 1. The historical pre-v801 NULL
    rows are out of scope — their transcripts may no longer exist on
    disk.
    """
    conn = get_connection(db_path)
    try:
        review = conn.execute(
            "SELECT id, task_id, created_at, cost_dollars, tokens_in, tokens_out"
            " FROM code_reviews WHERE id = ?",
            (args.review_id,),
        ).fetchone()
        if not review:
            print(f"Error: Review {args.review_id} not found", file=sys.stderr)
            return 2

        if not getattr(args, "force", False) and review["cost_dollars"] is not None:
            print(
                f"Review #{args.review_id} already has cost_dollars=${review['cost_dollars']:.4f}. "
                "Pass --force to overwrite.",
                file=sys.stderr,
            )
            return 1

        computed = _compute_review_cost_from_window(review["created_at"])
        if computed is None:
            print(
                f"Warning: No transcript with requests in window "
                f"[{review['created_at']}, now] for review #{args.review_id} — leaving columns unchanged.",
                file=sys.stderr,
            )
            return 1

        conn.execute(
            "UPDATE code_reviews SET cost_dollars = ?, tokens_in = ?, tokens_out = ?,"
            " updated_at = datetime('now') WHERE id = ?",
            (computed["cost_dollars"], computed["tokens_in"], computed["tokens_out"], args.review_id),
        )
        conn.commit()
    finally:
        conn.close()

    print(
        f"Review #{args.review_id} backfilled: "
        f"cost=${computed['cost_dollars']:.4f}, "
        f"tokens_in={computed['tokens_in']:,}, tokens_out={computed['tokens_out']:,}"
    )
    return 0


def cmd_status(args: argparse.Namespace, db_path: str) -> int:
    """Return JSON with per-reviewer status and comment counts for a task."""
    conn = get_connection(db_path)
    try:
        task = conn.execute(
            "SELECT id, summary FROM tasks WHERE id = ?", (args.task_id,)
        ).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        reviews = conn.execute(
            "SELECT r.id, r.reviewer, r.status, r.review_pass, r.created_at, r.updated_at,"
            "  COUNT(c.id) as total_comments,"
            "  SUM(CASE WHEN c.id IS NOT NULL AND c.resolution IS NULL THEN 1 ELSE 0 END) as open_comments,"
            "  SUM(CASE WHEN c.id IS NOT NULL AND c.resolution = 'fixed' THEN 1 ELSE 0 END) as fixed_comments,"
            "  SUM(CASE WHEN c.id IS NOT NULL AND c.resolution = 'deferred' THEN 1 ELSE 0 END) as deferred_comments,"
            "  SUM(CASE WHEN c.id IS NOT NULL AND c.resolution = 'dismissed' THEN 1 ELSE 0 END) as dismissed_comments"
            " FROM code_reviews r"
            " LEFT JOIN review_comments c ON c.review_id = r.id"
            " WHERE r.task_id = ? AND r.status <> 'superseded'"
            " GROUP BY r.id ORDER BY r.id",
            (args.task_id,),
        ).fetchall()
    finally:
        conn.close()

    result = {
        "task_id": args.task_id,
        "task_summary": task["summary"],
        "reviews": [
            {
                "review_id": r["id"],
                "reviewer": r["reviewer"],
                "status": r["status"],
                "review_pass": r["review_pass"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "comment_counts": {
                    "total": r["total_comments"] or 0,
                    "open": r["open_comments"] or 0,
                    "fixed": r["fixed_comments"] or 0,
                    "deferred": r["deferred_comments"] or 0,
                    "dismissed": r["dismissed_comments"] or 0,
                },
            }
            for r in reviews
        ],
    }

    print(dumps(result))
    return 0


def cmd_verdict(args: argparse.Namespace, db_path: str) -> int:
    """Return JSON verdict for a task based on open must_fix review comments."""
    conn = get_connection(db_path)
    try:
        task = conn.execute(
            "SELECT id FROM tasks WHERE id = ?", (args.task_id,)
        ).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        row = conn.execute(
            "SELECT COUNT(*) as cnt"
            " FROM review_comments rc"
            " JOIN code_reviews cr ON cr.id = rc.review_id"
            " WHERE cr.task_id = ? AND cr.status <> 'superseded'"
            " AND rc.category = 'must_fix' AND rc.resolution IS NULL",
            (args.task_id,),
        ).fetchone()
    finally:
        conn.close()

    open_must_fix = row["cnt"] if row else 0
    verdict = "APPROVED" if open_must_fix == 0 else "CHANGES_REMAINING"
    print(json.dumps({"verdict": verdict, "open_must_fix": open_must_fix}))
    return 0


def cmd_pass_status(args: argparse.Namespace, db_path: str, config_path: str) -> int:
    """Return JSON with current pass, max passes, can_retry, and open must_fix count."""
    conn = get_connection(db_path)
    try:
        task = conn.execute("SELECT id FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        pass_row = conn.execute(
            "SELECT review_pass FROM code_reviews"
            " WHERE task_id = ? AND status <> 'superseded' ORDER BY id DESC LIMIT 1",
            (args.task_id,),
        ).fetchone()
        current_pass = pass_row["review_pass"] if pass_row and pass_row["review_pass"] is not None else 0

        must_fix_row = conn.execute(
            "SELECT COUNT(*) as cnt"
            " FROM review_comments rc"
            " JOIN code_reviews cr ON cr.id = rc.review_id"
            " WHERE cr.task_id = ? AND cr.status <> 'superseded'"
            " AND rc.category = 'must_fix' AND rc.resolution IS NULL",
            (args.task_id,),
        ).fetchone()
    finally:
        conn.close()

    open_must_fix = must_fix_row["cnt"] if must_fix_row else 0
    cfg = load_review_config(config_path)
    max_passes = cfg["max_passes"]
    can_retry = current_pass < max_passes and open_must_fix > 0

    print(json.dumps({
        "current_pass": current_pass,
        "max_passes": max_passes,
        "can_retry": can_retry,
        "open_must_fix": open_must_fix,
    }))
    return 0


def cmd_summary(args: argparse.Namespace, db_path: str) -> int:
    """Output a summary of all findings for a review."""
    conn = get_connection(db_path)
    try:
        review = conn.execute(
            "SELECT r.id, r.task_id, r.reviewer, r.status, r.review_pass,"
            "  r.diff_summary, r.created_at, t.summary as task_summary"
            " FROM code_reviews r JOIN tasks t ON t.id = r.task_id"
            " WHERE r.id = ?",
            (args.review_id,),
        ).fetchone()
        if not review:
            print(f"Error: Review {args.review_id} not found", file=sys.stderr)
            return 2

        comments = conn.execute(
            "SELECT id, file_path, line_start, line_end, category, severity, comment, resolution"
            " FROM review_comments WHERE review_id = ? ORDER BY severity, category, id",
            (args.review_id,),
        ).fetchall()
    finally:
        conn.close()

    reviewer_label = review["reviewer"] or "unassigned"
    verdict = "APPROVED" if review["status"] == "approved" else (
        "CHANGES REQUESTED" if review["status"] == "changes_requested" else review["status"].upper()
    )

    print(f"Review #{review['id']} Summary")
    print(f"Task:     #{review['task_id']} {review['task_summary']}")
    print(f"Reviewer: {reviewer_label}")
    print(f"Status:   {verdict} (pass {review['review_pass']})")
    print(f"Date:     {review['created_at']}")
    if review["diff_summary"]:
        print(f"Diff:     {review['diff_summary']}")
    print()

    if not comments:
        print("No findings.")
        return 0

    open_comments = [c for c in comments if c["resolution"] is None]
    resolved_comments = [c for c in comments if c["resolution"] is not None]

    print(f"Findings: {len(comments)} total, {len(open_comments)} open, {len(resolved_comments)} resolved")
    print()

    if open_comments:
        print("Open findings:")
        for c in open_comments:
            loc = ""
            if c["file_path"]:
                loc = f" {c['file_path']}"
                if c["line_start"]:
                    loc += f":{c['line_start']}"
                    if c["line_end"] and c["line_end"] != c["line_start"]:
                        loc += f"-{c['line_end']}"
            cat = f"[{c['category']}]" if c["category"] else ""
            sev = f"[{c['severity']}]" if c["severity"] else ""
            tags = " ".join(x for x in [cat, sev] if x)
            tags_str = f" {tags}" if tags else ""
            print(f"  #{c['id']}{loc}{tags_str}: {c['comment']}")
        print()

    if resolved_comments:
        print("Resolved findings:")
        for c in resolved_comments:
            loc = ""
            if c["file_path"]:
                loc = f" {c['file_path']}"
                if c["line_start"]:
                    loc += f":{c['line_start']}"
            print(f"  #{c['id']}{loc} ({c['resolution']}): {c['comment']}")
        print()

    return 0


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print("Usage: tusk review {start|begin|add-comment|list|resolve|approve|request-changes|status|summary} ...", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    config_path = sys.argv[2]

    parser = argparse.ArgumentParser(
        prog="tusk review",
        description="Manage code reviews for tasks",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # start
    start_p = subparsers.add_parser("start", help="Start a new code review for a task")
    start_p.add_argument("task_id", type=int, help="Task ID")
    start_p.add_argument("--reviewer", default=None, help="Reviewer name (overrides config reviewers)")
    start_p.add_argument("--pass-num", type=int, default=1, help="Review pass number (default: 1)")
    start_p.add_argument("--diff-summary", default=None, help="Optional diff summary text")
    start_p.add_argument("--agent", default=None, help="Agent name that ran the review (e.g. from /chain)")

    # begin
    begin_p = subparsers.add_parser(
        "begin",
        help="Bundle review-diff-range and review start in one call (returns JSON)",
    )
    begin_p.add_argument("task_id", type=int, help="Task ID")
    begin_p.add_argument("--reviewer", default=None, help="Reviewer name (overrides config reviewers)")
    begin_p.add_argument("--pass-num", type=int, default=1, help="Review pass number (default: 1)")
    begin_p.add_argument("--agent", default=None, help="Agent name that ran the review (e.g. from /chain)")

    # add-comment
    add_comment_p = subparsers.add_parser("add-comment", help="Add a finding comment to a review")
    add_comment_p.add_argument("review_id", type=int, help="Review ID")
    add_comment_p.add_argument("comment", help="Comment text")
    add_comment_p.add_argument("--file", default=None, help="File path")
    add_comment_p.add_argument("--line-start", type=int, default=None, help="Starting line number")
    add_comment_p.add_argument("--line-end", type=int, default=None, help="Ending line number")
    add_comment_p.add_argument("--category", default=None, help="Finding category (e.g., must_fix, suggest, defer)")
    add_comment_p.add_argument("--severity", default=None, help="Severity (e.g., critical, major, minor)")

    # list
    list_p = subparsers.add_parser("list", help="List reviews and findings for a task")
    list_p.add_argument("task_id", type=int, help="Task ID")

    # resolve
    resolve_p = subparsers.add_parser("resolve", help="Resolve a review comment")
    resolve_p.add_argument("comment_id", type=int, help="Comment ID")
    resolve_p.add_argument("resolution", choices=["fixed", "deferred", "dismissed"], help="Resolution status")
    resolve_p.add_argument(
        "--deferred-task-id",
        dest="deferred_task_id",
        type=int,
        default=None,
        help="Task ID created from this deferred finding (only valid with resolution='deferred')",
    )

    # approve
    approve_p = subparsers.add_parser("approve", help="Approve a review")
    approve_p.add_argument("review_id", type=int, help="Review ID")
    approve_p.add_argument(
        "--note",
        help="Optional reason or note to store with the approval. Pass --note '' to clear an existing note.",
    )
    approve_p.add_argument(
        "--model",
        help="Reviewer model ID (e.g. claude-opus-4-7). Pass --model '' to clear an existing model.",
    )
    _add_cost_flags(approve_p)

    # request-changes
    req_changes_p = subparsers.add_parser("request-changes", help="Request changes on a review")
    req_changes_p.add_argument("review_id", type=int, help="Review ID")
    req_changes_p.add_argument(
        "--note",
        help="Optional reason or note to store with the changes-requested verdict. Pass --note '' to clear an existing note.",
    )
    req_changes_p.add_argument(
        "--model",
        help="Reviewer model ID (e.g. claude-opus-4-7). Pass --model '' to clear an existing model.",
    )
    _add_cost_flags(req_changes_p)

    # backfill-cost
    backfill_cost_p = subparsers.add_parser(
        "backfill-cost",
        help="Recompute cost/tokens for an existing review row from its created_at window",
    )
    backfill_cost_p.add_argument("review_id", type=int, help="Review ID")
    backfill_cost_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing cost_dollars even if it is already populated",
    )

    # status
    status_p = subparsers.add_parser("status", help="Show current review status for a task (JSON)")
    status_p.add_argument("task_id", type=int, help="Task ID")

    # summary
    summary_p = subparsers.add_parser("summary", help="Print a human-readable summary of a review")
    summary_p.add_argument("review_id", type=int, help="Review ID")

    # verdict
    verdict_p = subparsers.add_parser("verdict", help="Return JSON verdict for a task (APPROVED or CHANGES_REMAINING)")
    verdict_p.add_argument("task_id", type=int, help="Task ID")

    # pass-status
    pass_status_p = subparsers.add_parser("pass-status", help="Return JSON with current pass, max passes, can_retry, open_must_fix")
    pass_status_p.add_argument("task_id", type=int, help="Task ID")

    args = parser.parse_args(sys.argv[3:])

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "start":
            sys.exit(cmd_start(args, db_path, config_path))
        elif args.command == "begin":
            sys.exit(cmd_begin(args, db_path, config_path))
        elif args.command == "add-comment":
            sys.exit(cmd_add_comment(args, db_path, config_path))
        elif args.command == "list":
            sys.exit(cmd_list(args, db_path))
        elif args.command == "resolve":
            sys.exit(cmd_resolve(args, db_path))
        elif args.command == "approve":
            sys.exit(cmd_approve(args, db_path))
        elif args.command == "request-changes":
            sys.exit(cmd_request_changes(args, db_path))
        elif args.command == "backfill-cost":
            sys.exit(cmd_backfill_cost(args, db_path))
        elif args.command == "status":
            sys.exit(cmd_status(args, db_path))
        elif args.command == "summary":
            sys.exit(cmd_summary(args, db_path))
        elif args.command == "verdict":
            sys.exit(cmd_verdict(args, db_path))
        elif args.command == "pass-status":
            sys.exit(cmd_pass_status(args, db_path, config_path))
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
