#!/usr/bin/env python3
"""Fuzzy duplicate detection for tusk task databases.

Called by the tusk wrapper:
    tusk dupes check|scan|similar ...

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — subcommand + flags
"""

import argparse
import json
import re
import sqlite3
import sys
from difflib import SequenceMatcher

# ── Config-driven globals (set in main()) ────────────────────────────

DEFAULT_CHECK_THRESHOLD = 0.82
DEFAULT_SIMILAR_THRESHOLD = 0.6
PREFIX_PATTERN: re.Pattern = re.compile(r"$^")  # replaced at startup
TERMINAL_STATUS = "Done"


def load_config(config_path: str) -> None:
    """Load dupes settings from config and set module globals."""
    global DEFAULT_CHECK_THRESHOLD, DEFAULT_SIMILAR_THRESHOLD
    global PREFIX_PATTERN, TERMINAL_STATUS

    with open(config_path) as f:
        cfg = json.load(f)

    dupes = cfg.get("dupes", {})
    DEFAULT_CHECK_THRESHOLD = dupes.get("check_threshold", DEFAULT_CHECK_THRESHOLD)
    DEFAULT_SIMILAR_THRESHOLD = dupes.get("similar_threshold", DEFAULT_SIMILAR_THRESHOLD)

    # Build prefix pattern from config + generic JIRA pattern
    prefixes = dupes.get("strip_prefixes", ["Deferred", "Enhancement", "Optional"])
    parts = [re.escape(p) for p in prefixes] + [r"[A-Z]+-\d+"]
    prefix_alt = "|".join(parts)
    PREFIX_PATTERN = re.compile(
        rf"^\s*(\[(?:{prefix_alt})\]\s*)+", re.IGNORECASE
    )

    # Terminal status is the last entry in the statuses list
    statuses = cfg.get("statuses", ["To Do", "In Progress", "Done"])
    TERMINAL_STATUS = statuses[-1] if statuses else "Done"


# ── Helpers ──────────────────────────────────────────────────────────

def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_summary(summary: str) -> str:
    text = PREFIX_PATTERN.sub("", summary)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_summary(a), normalize_summary(b)).ratio()


def get_open_tasks(
    conn: sqlite3.Connection,
    domain: str | None = None,
    status: str | None = None,
) -> list[sqlite3.Row]:
    query = f"SELECT id, summary, domain, status, priority FROM tasks WHERE status != ?"
    params: list[str] = [TERMINAL_STATUS]
    if domain:
        query += " AND domain = ?"
        params.append(domain)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY id"
    return conn.execute(query, params).fetchall()


# ── Subcommands ──────────────────────────────────────────────────────

def cmd_check(args: argparse.Namespace, db_path: str) -> int:
    conn = get_connection(db_path)
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


def cmd_scan(args: argparse.Namespace, db_path: str) -> int:
    conn = get_connection(db_path)
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


def cmd_similar(args: argparse.Namespace, db_path: str) -> int:
    conn = get_connection(db_path)

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


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print("Usage: tusk dupes {check|scan|similar} ...", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    config_path = sys.argv[2]
    load_config(config_path)

    parser = argparse.ArgumentParser(
        prog="tusk dupes",
        description="Fuzzy duplicate detection for tusk tasks",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # check
    check_p = subparsers.add_parser("check", help="Check a summary against open tasks")
    check_p.add_argument("summary", help="Task summary to check")
    check_p.add_argument("--domain", help="Filter to a specific domain")
    check_p.add_argument(
        "--threshold", type=float, default=DEFAULT_CHECK_THRESHOLD,
        help=f"Similarity threshold (default: {DEFAULT_CHECK_THRESHOLD})",
    )
    check_p.add_argument("--json", action="store_true", help="Output JSON")

    # scan
    scan_p = subparsers.add_parser("scan", help="Find all duplicate pairs among open tasks")
    scan_p.add_argument("--domain", help="Filter to a specific domain")
    scan_p.add_argument("--status", help="Filter to a specific status")
    scan_p.add_argument(
        "--threshold", type=float, default=DEFAULT_CHECK_THRESHOLD,
        help=f"Similarity threshold (default: {DEFAULT_CHECK_THRESHOLD})",
    )
    scan_p.add_argument("--json", action="store_true", help="Output JSON")

    # similar
    sim_p = subparsers.add_parser("similar", help="Find tasks similar to a given task ID")
    sim_p.add_argument("id", type=int, help="Task ID")
    sim_p.add_argument("--domain", help="Filter to a specific domain")
    sim_p.add_argument(
        "--threshold", type=float, default=DEFAULT_SIMILAR_THRESHOLD,
        help=f"Similarity threshold (default: {DEFAULT_SIMILAR_THRESHOLD})",
    )
    sim_p.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args(sys.argv[3:])

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        handlers = {"check": cmd_check, "scan": cmd_scan, "similar": cmd_similar}
        sys.exit(handlers[args.command](args, db_path))
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
