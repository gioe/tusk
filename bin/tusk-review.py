#!/usr/bin/env python3
"""Manage code reviews for tusk tasks.

Called by the tusk wrapper:
    tusk review start|add-comment|list|resolve|approve|request-changes|status|summary ...

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — subcommand + flags
"""

import argparse
import json
import sqlite3
import sys


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_review_config(config_path: str) -> dict:
    """Load review-related config values."""
    try:
        with open(config_path) as f:
            config = json.load(f)
        return {
            "reviewers": config.get("review", {}).get("reviewers", []),
            "categories": config.get("review_categories", []),
            "severities": config.get("review_severities", []),
        }
    except (OSError, json.JSONDecodeError):
        return {"reviewers": [], "categories": [], "severities": []}


def cmd_start(args: argparse.Namespace, db_path: str, config_path: str) -> int:
    """Create one code_reviews row per enabled reviewer (or a single unassigned row)."""
    conn = get_connection(db_path)
    try:
        task = conn.execute("SELECT id, summary FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        cfg = load_review_config(config_path)
        reviewers = cfg["reviewers"]

        # If a specific reviewer was passed on the CLI, use only that one
        if args.reviewer:
            reviewers = [args.reviewer]

        # If no reviewers configured and none specified, create one unassigned review
        if not reviewers:
            reviewers = [None]

        created_ids = []
        for reviewer_item in reviewers:
            # reviewer_item may be a dict {"name": ..., "description": ...} or a plain string/None
            if isinstance(reviewer_item, dict):
                reviewer_name = reviewer_item.get("name")
            else:
                reviewer_name = reviewer_item
            conn.execute(
                "INSERT INTO code_reviews (task_id, reviewer, status, review_pass, diff_summary, agent_name)"
                " VALUES (?, ?, 'pending', ?, ?, ?)",
                (args.task_id, reviewer_name, args.pass_num, args.diff_summary, args.agent),
            )
            conn.commit()
            rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            created_ids.append((rid, reviewer_name))
    finally:
        conn.close()

    for rid, reviewer in created_ids:
        reviewer_str = f" (reviewer: {reviewer})" if reviewer else ""
        print(f"Started review #{rid} for task #{args.task_id}{reviewer_str}: {task['summary']}")

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
            " FROM code_reviews WHERE task_id = ? ORDER BY id",
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

    conn = get_connection(db_path)
    try:
        comment = conn.execute(
            "SELECT id, comment, resolution FROM review_comments WHERE id = ?",
            (args.comment_id,),
        ).fetchone()
        if not comment:
            print(f"Error: Comment {args.comment_id} not found", file=sys.stderr)
            return 2

        conn.execute(
            "UPDATE review_comments SET resolution = ?, updated_at = datetime('now') WHERE id = ?",
            (args.resolution, args.comment_id),
        )
        conn.commit()
    finally:
        conn.close()

    print(f"Comment #{args.comment_id} marked '{args.resolution}': {comment['comment'][:60]}")
    return 0


def cmd_approve(args: argparse.Namespace, db_path: str) -> int:
    """Set code_reviews.status = 'approved' and review_pass = 1."""
    conn = get_connection(db_path)
    try:
        review = conn.execute(
            "SELECT id, task_id, reviewer, status FROM code_reviews WHERE id = ?",
            (args.review_id,),
        ).fetchone()
        if not review:
            print(f"Error: Review {args.review_id} not found", file=sys.stderr)
            return 2

        conn.execute(
            "UPDATE code_reviews SET status = 'approved', review_pass = 1,"
            " updated_at = datetime('now') WHERE id = ?",
            (args.review_id,),
        )
        conn.commit()
    finally:
        conn.close()

    reviewer_str = f" by {review['reviewer']}" if review["reviewer"] else ""
    print(f"Review #{args.review_id} approved{reviewer_str} for task #{review['task_id']}")
    return 0


def cmd_request_changes(args: argparse.Namespace, db_path: str) -> int:
    """Set code_reviews.status = 'changes_requested' and review_pass = 0."""
    conn = get_connection(db_path)
    try:
        review = conn.execute(
            "SELECT id, task_id, reviewer, status FROM code_reviews WHERE id = ?",
            (args.review_id,),
        ).fetchone()
        if not review:
            print(f"Error: Review {args.review_id} not found", file=sys.stderr)
            return 2

        conn.execute(
            "UPDATE code_reviews SET status = 'changes_requested', review_pass = 0,"
            " updated_at = datetime('now') WHERE id = ?",
            (args.review_id,),
        )
        conn.commit()
    finally:
        conn.close()

    reviewer_str = f" by {review['reviewer']}" if review["reviewer"] else ""
    print(f"Review #{args.review_id} changes requested{reviewer_str} for task #{review['task_id']}")
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
            "  SUM(CASE WHEN c.resolution IS NULL THEN 1 ELSE 0 END) as open_comments,"
            "  SUM(CASE WHEN c.resolution = 'fixed' THEN 1 ELSE 0 END) as fixed_comments,"
            "  SUM(CASE WHEN c.resolution = 'deferred' THEN 1 ELSE 0 END) as deferred_comments,"
            "  SUM(CASE WHEN c.resolution = 'dismissed' THEN 1 ELSE 0 END) as dismissed_comments"
            " FROM code_reviews r"
            " LEFT JOIN review_comments c ON c.review_id = r.id"
            " WHERE r.task_id = ?"
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

    print(json.dumps(result, indent=2))
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
        print("Usage: tusk review {start|add-comment|list|resolve|approve|request-changes|status|summary} ...", file=sys.stderr)
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

    # approve
    approve_p = subparsers.add_parser("approve", help="Approve a review")
    approve_p.add_argument("review_id", type=int, help="Review ID")

    # request-changes
    req_changes_p = subparsers.add_parser("request-changes", help="Request changes on a review")
    req_changes_p.add_argument("review_id", type=int, help="Review ID")

    # status
    status_p = subparsers.add_parser("status", help="Show current review status for a task (JSON)")
    status_p.add_argument("task_id", type=int, help="Task ID")

    # summary
    summary_p = subparsers.add_parser("summary", help="Print a human-readable summary of a review")
    summary_p.add_argument("review_id", type=int, help="Review ID")

    args = parser.parse_args(sys.argv[3:])

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "start":
            sys.exit(cmd_start(args, db_path, config_path))
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
        elif args.command == "status":
            sys.exit(cmd_status(args, db_path))
        elif args.command == "summary":
            sys.exit(cmd_summary(args, db_path))
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
