#!/usr/bin/env python3
"""Check for duplicate tasks in the SQLite database."""

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from difflib import SequenceMatcher

DB_PATH = subprocess.check_output(
    [".claude/bin/tusk", "path"], text=True
).strip()

DEFAULT_THRESHOLD = 0.82
SIMILAR_THRESHOLD = 0.6

# Prefixes stripped before comparison
PREFIX_PATTERN = re.compile(
    r"^\s*(\[(?:Deferred|Enhancement|Optional|ICG-\d+)\]\s*)+", re.IGNORECASE
)


def get_connection() -> sqlite3.Connection:
    """Get database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_summary(summary: str) -> str:
    """Strip tag prefixes, collapse whitespace, and lowercase."""
    text = PREFIX_PATTERN.sub("", summary)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def similarity(a: str, b: str) -> float:
    """Compute similarity ratio on normalized summaries."""
    return SequenceMatcher(None, normalize_summary(a), normalize_summary(b)).ratio()


def get_open_tasks(
    conn: sqlite3.Connection,
    domain: str | None = None,
    status: str | None = None,
) -> list[sqlite3.Row]:
    """Fetch open tasks, optionally filtered by domain or status."""
    query = "SELECT id, summary, domain, status, priority FROM tasks WHERE status != 'Done'"
    params: list[str] = []
    if domain:
        query += " AND domain = ?"
        params.append(domain)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY id"
    return conn.execute(query, params).fetchall()


def cmd_check(args: argparse.Namespace) -> int:
    """Check a summary against existing open tasks for duplicates."""
    conn = get_connection()
    tasks = get_open_tasks(conn, domain=args.domain)
    conn.close()

    matches = []
    for task in tasks:
        score = similarity(args.summary, task["summary"])
        if score >= args.threshold:
            matches.append(
                {
                    "id": task["id"],
                    "summary": task["summary"],
                    "domain": task["domain"],
                    "similarity": round(score, 3),
                }
            )

    matches.sort(key=lambda m: m["similarity"], reverse=True)

    if args.json:
        print(json.dumps({"duplicates": matches}, indent=2))
    elif matches:
        print(f"Duplicates found for: {args.summary!r}")
        print(f"{'ID':<6} {'Score':<7} {'Summary'}")
        print("-" * 70)
        for m in matches:
            print(f"{m['id']:<6} {m['similarity']:<7.3f} {m['summary']}")
    else:
        print(f"No duplicates found for: {args.summary!r}")

    return 1 if matches else 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Find all duplicate pairs among open tasks."""
    conn = get_connection()
    tasks = get_open_tasks(conn, domain=args.domain, status=args.status)
    conn.close()

    pairs = []
    seen = set()
    for i, t1 in enumerate(tasks):
        for t2 in tasks[i + 1 :]:
            score = similarity(t1["summary"], t2["summary"])
            if score >= args.threshold:
                key = (min(t1["id"], t2["id"]), max(t1["id"], t2["id"]))
                if key not in seen:
                    seen.add(key)
                    pairs.append(
                        {
                            "task_a": {"id": t1["id"], "summary": t1["summary"]},
                            "task_b": {"id": t2["id"], "summary": t2["summary"]},
                            "similarity": round(score, 3),
                        }
                    )

    pairs.sort(key=lambda p: p["similarity"], reverse=True)

    if args.json:
        print(json.dumps({"duplicate_pairs": pairs}, indent=2))
    elif pairs:
        print(f"Duplicate pairs found: {len(pairs)}")
        print(f"{'ID A':<6} {'ID B':<6} {'Score':<7} {'Summary A':<35} {'Summary B'}")
        print("-" * 90)
        for p in pairs:
            print(
                f"{p['task_a']['id']:<6} "
                f"{p['task_b']['id']:<6} "
                f"{p['similarity']:<7.3f} "
                f"{p['task_a']['summary'][:35]:<35} "
                f"{p['task_b']['summary'][:35]}"
            )
    else:
        print("No duplicate pairs found.")

    return 1 if pairs else 0


def cmd_similar(args: argparse.Namespace) -> int:
    """Find tasks similar to a given task ID."""
    conn = get_connection()

    target = conn.execute(
        "SELECT id, summary, domain FROM tasks WHERE id = ?", (args.id,)
    ).fetchone()
    if not target:
        print(f"Error: Task {args.id} not found", file=sys.stderr)
        conn.close()
        return 2

    tasks = get_open_tasks(conn, domain=args.domain)
    conn.close()

    matches = []
    for task in tasks:
        if task["id"] == target["id"]:
            continue
        score = similarity(target["summary"], task["summary"])
        if score >= args.threshold:
            matches.append(
                {
                    "id": task["id"],
                    "summary": task["summary"],
                    "domain": task["domain"],
                    "similarity": round(score, 3),
                }
            )

    matches.sort(key=lambda m: m["similarity"], reverse=True)

    if args.json:
        print(
            json.dumps(
                {"target": {"id": target["id"], "summary": target["summary"]}, "similar": matches},
                indent=2,
            )
        )
    elif matches:
        print(f"Tasks similar to #{target['id']}: {target['summary']!r}")
        print(f"{'ID':<6} {'Score':<7} {'Domain':<18} {'Summary'}")
        print("-" * 80)
        for m in matches:
            print(f"{m['id']:<6} {m['similarity']:<7.3f} {m['domain'] or 'N/A':<18} {m['summary']}")
    else:
        print(f"No similar tasks found for #{target['id']}: {target['summary']!r}")

    return 1 if matches else 0


def main():
    parser = argparse.ArgumentParser(
        description="Check for duplicate tasks in the SQLite database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s check "Add error handling for delete account" --domain iOS
  %(prog)s scan --status "To Do"
  %(prog)s similar 42
  %(prog)s check "unique task" --json
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # check command
    check_parser = subparsers.add_parser(
        "check", help="Check a summary against open tasks for duplicates"
    )
    check_parser.add_argument("summary", help="Task summary to check")
    check_parser.add_argument("--domain", help="Filter to a specific domain")
    check_parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Similarity threshold (default: {DEFAULT_THRESHOLD})",
    )
    check_parser.add_argument("--json", action="store_true", help="Output JSON")

    # scan command
    scan_parser = subparsers.add_parser(
        "scan", help="Find all duplicate pairs among open tasks"
    )
    scan_parser.add_argument("--domain", help="Filter to a specific domain")
    scan_parser.add_argument("--status", help="Filter to a specific status")
    scan_parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Similarity threshold (default: {DEFAULT_THRESHOLD})",
    )
    scan_parser.add_argument("--json", action="store_true", help="Output JSON")

    # similar command
    similar_parser = subparsers.add_parser(
        "similar", help="Find tasks similar to a given task ID"
    )
    similar_parser.add_argument("id", type=int, help="Task ID to find similar tasks for")
    similar_parser.add_argument("--domain", help="Filter to a specific domain")
    similar_parser.add_argument(
        "--threshold",
        type=float,
        default=SIMILAR_THRESHOLD,
        help=f"Similarity threshold (default: {SIMILAR_THRESHOLD})",
    )
    similar_parser.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "check":
            sys.exit(cmd_check(args))
        elif args.command == "scan":
            sys.exit(cmd_scan(args))
        elif args.command == "similar":
            sys.exit(cmd_similar(args))
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
