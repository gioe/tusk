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
import logging
import re
import sqlite3
import sys
from difflib import SequenceMatcher

log = logging.getLogger(__name__)

# ── Config-driven globals (set in main()) ────────────────────────────

DEFAULT_CHECK_THRESHOLD = 0.82
DEFAULT_SIMILAR_THRESHOLD = 0.6
PREFIX_PATTERN: re.Pattern = re.compile(r"$^")  # replaced at startup
TERMINAL_STATUS = "Done"


def load_config(config_path: str) -> None:
    """Load dupes settings from config and set module globals."""
    global DEFAULT_CHECK_THRESHOLD, DEFAULT_SIMILAR_THRESHOLD
    global PREFIX_PATTERN, TERMINAL_STATUS

    log.debug("Loading config from %s", config_path)
    with open(config_path) as f:
        cfg = json.load(f)

    dupes = cfg.get("dupes", {})
    DEFAULT_CHECK_THRESHOLD = dupes.get("check_threshold", DEFAULT_CHECK_THRESHOLD)
    DEFAULT_SIMILAR_THRESHOLD = dupes.get("similar_threshold", DEFAULT_SIMILAR_THRESHOLD)
    log.debug("Thresholds — check: %s, similar: %s",
              DEFAULT_CHECK_THRESHOLD, DEFAULT_SIMILAR_THRESHOLD)

    # Build prefix pattern from config + generic JIRA pattern
    prefixes = dupes.get("strip_prefixes", ["Deferred", "Enhancement", "Optional"])
    parts = [re.escape(p) for p in prefixes] + [r"[A-Z]+-\d+"]
    prefix_alt = "|".join(parts)
    PREFIX_PATTERN = re.compile(
        rf"^\s*(\[(?:{prefix_alt})\]\s*)+", re.IGNORECASE
    )
    log.debug("Strip-prefix pattern: %s", PREFIX_PATTERN.pattern)

    # Terminal status is the last entry in the statuses list
    statuses = cfg.get("statuses", ["To Do", "In Progress", "Done"])
    TERMINAL_STATUS = statuses[-1] if statuses else "Done"
    log.debug("Terminal status: %s", TERMINAL_STATUS)


# ── Helpers ──────────────────────────────────────────────────────────

def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_summary(summary: str) -> str:
    text = PREFIX_PATTERN.sub("", summary)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def tokenize(text: str) -> set[str]:
    """Split normalized text into a set of word tokens.

    Preserves compound tokens (e.g. ``bin/tusk``, ``tusk-dupes.py``) by
    splitting only on whitespace.  The input should already be
    normalized (lowercased, prefix-stripped, whitespace-collapsed).

    Leading slashes are stripped so that ``/tusk`` and ``tusk``
    are treated as the same token.
    """
    return {tok.lstrip("/") for tok in text.split()}


def char_similarity(norm_a: str, norm_b: str) -> float:
    """Character-level similarity using SequenceMatcher."""
    return SequenceMatcher(None, norm_a, norm_b).ratio()


def token_similarity(norm_a: str, norm_b: str) -> float:
    """Token-level (Jaccard) similarity between two normalized strings.

    Tokenizes each string into word sets and returns
    ``|intersection| / |union|``.  Returns 0.0 when both inputs are empty.
    """
    set_a = tokenize(norm_a)
    set_b = tokenize(norm_b)
    if not set_a and not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


# Weight applied to the token (Jaccard) score when blending.
TOKEN_WEIGHT = 0.3
CHAR_WEIGHT = 1.0 - TOKEN_WEIGHT


def combined_similarity(norm_a: str, norm_b: str) -> float:
    """Blended similarity: character-level + token-level (Jaccard).

    Returns a weighted average of the two scores so that tasks sharing
    key terms (but differing in phrasing) score higher than pure
    character-level comparison alone.
    """
    return (CHAR_WEIGHT * char_similarity(norm_a, norm_b)
            + TOKEN_WEIGHT * token_similarity(norm_a, norm_b))


def similarity(a: str, b: str) -> float:
    return combined_similarity(normalize_summary(a), normalize_summary(b))


def build_norm_cache(tasks) -> dict[int, str]:
    """Pre-compute normalized summaries for a list of tasks."""
    return {t["id"]: normalize_summary(t["summary"]) for t in tasks}


def similarity_cached(norm_a: str, norm_b: str) -> float:
    """Compute combined similarity between two pre-normalized strings."""
    return combined_similarity(norm_a, norm_b)


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
    log.debug("SQL: %s  params: %s", query, params)
    tasks = conn.execute(query, params).fetchall()
    log.debug("Loaded %d open tasks", len(tasks))
    return tasks


# ── Subcommands ──────────────────────────────────────────────────────

def cmd_check(args: argparse.Namespace, db_path: str) -> int:
    log.debug("cmd_check: summary=%r, domain=%s, threshold=%s",
              args.summary, args.domain, args.threshold)
    conn = get_connection(db_path)
    try:
        tasks = get_open_tasks(conn, domain=args.domain)
    finally:
        conn.close()

    norm_input = normalize_summary(args.summary)
    log.debug("Normalized input: %r", norm_input)
    cache = build_norm_cache(tasks)

    matches = []
    for task in tasks:
        score = similarity_cached(norm_input, cache[task["id"]])
        log.debug("  Task #%d: score=%.3f norm=%r", task["id"], score, cache[task["id"]])
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
    log.debug("cmd_scan: domain=%s, status=%s, threshold=%s",
              args.domain, args.status, args.threshold)
    conn = get_connection(db_path)
    try:
        tasks = get_open_tasks(conn, domain=args.domain, status=args.status)
    finally:
        conn.close()

    cache = build_norm_cache(tasks)
    log.debug("Comparing %d tasks (%d pairs)", len(tasks), len(tasks) * (len(tasks) - 1) // 2)

    pairs = []
    seen = set()
    for i, t1 in enumerate(tasks):
        for t2 in tasks[i + 1 :]:
            score = similarity_cached(cache[t1["id"]], cache[t2["id"]])
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
    log.debug("cmd_similar: id=%d, domain=%s, threshold=%s",
              args.id, args.domain, args.threshold)
    conn = get_connection(db_path)
    try:
        target = conn.execute(
            "SELECT id, summary, domain FROM tasks WHERE id = ?", (args.id,)
        ).fetchone()
        if not target:
            print(f"Error: Task {args.id} not found", file=sys.stderr)
            return 2

        tasks = get_open_tasks(conn, domain=args.domain)
    finally:
        conn.close()

    norm_target = normalize_summary(target["summary"])
    cache = build_norm_cache(tasks)

    matches = []
    for task in tasks:
        if task["id"] == target["id"]:
            continue
        score = similarity_cached(norm_target, cache[task["id"]])
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
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug output")
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

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="[debug] %(message)s",
        stream=sys.stderr,
    )
    log.debug("DB path: %s", db_path)
    log.debug("Config path: %s", config_path)

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
