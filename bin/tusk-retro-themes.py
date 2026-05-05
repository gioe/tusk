#!/usr/bin/env python3
"""Cross-retro theme rollup, grouped by content-derived key, emitted as JSON.

/retro consumes this (never raw `retro_findings` rows) so that every
cross-retro pattern check is done behind one helper — satisfies TASK-108
criterion 480.

Aggregation key is normalized topic terms extracted from
`retro_findings.summary` (single tokens and bigrams, with stop-words and
short tokens dropped). Counts reflect the number of distinct findings a
term appears in, not raw token frequency. Single-letter category codes
(A/B/C/D/E) are NOT used as themes — `category` was the prior key, but it
collapsed every retro into the same handful of buckets and yielded
tautological recurrence counts (issue #551).

Called by the tusk wrapper:
    tusk retro-themes [--window-days N] [--min-recurrence N]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path

Flags:
    --window-days N      Look-back window for `created_at` filter. Default 30.
                         0 means "all history" (no window filter).
    --min-recurrence N   Drop themes whose count is below N. Default 1 (emit
                         every theme). /retro passes 3 to surface only themes
                         appearing in 3+ findings in the window.

Output JSON shape (pre-aggregated tuples only — no raw row escape hatch):
    {
        "window_days": N,
        "min_recurrence": N,
        "total_findings": N,          # rows in the window; counted BEFORE
                                      # min_recurrence is applied so callers
                                      # can tell "6 findings, only 1 recurring
                                      # theme" at a glance
        "themes": [                   # sorted by count desc, then theme asc
            {"theme": "<topic>", "count": N},
            ...
        ]
    }

Exit codes:
    0 — success
    1 — error (bad arguments, DB issue)
"""

import argparse
import os
import re
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # noqa: E402

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection


DEFAULT_WINDOW_DAYS = 30
DEFAULT_MIN_RECURRENCE = 1
MIN_TERM_LENGTH = 4

# Common English stop-words plus a few tusk-domain noise terms that recur
# across most findings without conveying a topic ("task", "tusk", etc).
STOP_WORDS = frozenset({
    "a", "about", "above", "across", "after", "again", "against", "all",
    "also", "an", "and", "any", "are", "as", "at", "back", "be", "been",
    "before", "being", "below", "between", "both", "but", "by", "can",
    "could", "did", "do", "does", "doing", "done", "down", "during", "each",
    "even", "every", "few", "for", "from", "further", "had", "has", "have",
    "having", "he", "her", "here", "hers", "herself", "him", "himself",
    "his", "how", "however", "i", "if", "in", "into", "is", "it", "its",
    "itself", "just", "let", "like", "may", "me", "might", "more", "most",
    "much", "must", "my", "myself", "no", "nor", "not", "now", "of", "off",
    "on", "once", "only", "or", "other", "ought", "our", "ours", "ourselves",
    "out", "over", "own", "rather", "same", "she", "should", "since", "so",
    "some", "still", "such", "than", "that", "the", "their", "theirs",
    "them", "themselves", "then", "there", "these", "they", "this", "those",
    "though", "through", "to", "too", "under", "until", "up", "use", "used",
    "uses", "using", "very", "via", "was", "we", "were", "what", "when",
    "where", "which", "while", "who", "whom", "why", "will", "with",
    "within", "without", "would", "yet", "you", "your", "yours", "yourself",
    # tusk-domain noise — these appear in nearly every finding summary and
    # would otherwise dominate the recurrence ranking without naming a
    # specific theme.
    "task", "tusk", "skill", "skills", "step", "steps", "issue", "issues",
    "session", "make", "made", "need", "needs",
})


def _extract_terms(summary: str) -> set:
    """Return the set of normalized topic terms found in `summary`.

    A term is either a single content word or a bigram of consecutive content
    words. Tokens are lowercased, stripped of non-alphanumerics, and dropped
    if they are stop-words or shorter than MIN_TERM_LENGTH characters.

    Each term is counted once per finding (set semantics) regardless of how
    many times it appears within a single summary — recurrence is at the
    finding level, not the token level.
    """
    if not summary:
        return set()
    cleaned = re.sub(r"[^a-z0-9]+", " ", summary.lower())
    tokens = [
        t for t in cleaned.split()
        if t not in STOP_WORDS and len(t) >= MIN_TERM_LENGTH
    ]
    terms: set = set(tokens)
    for first, second in zip(tokens, tokens[1:]):
        terms.add(f"{first} {second}")
    return terms


def fetch_themes(
    conn: sqlite3.Connection,
    *,
    window_days: int,
    min_recurrence: int,
) -> dict:
    """Aggregate retro_findings.summary into content-derived themes.

    - window_days == 0 disables the date filter (all history).
    - window_days > 0 limits to rows whose created_at >= datetime('now', '-N days').
    - Rows in the window contribute their distinct topic terms (see
      `_extract_terms`) to a Counter; recurrence counts the number of
      findings each term appears in.
    - Themes whose recurrence is below `min_recurrence` are dropped.
    - total_findings is the raw row count in the window, computed before
      the recurrence filter, so callers can compare "rows in window" vs
      "themes that survived the floor".
    """
    params: list = []
    window_clause = ""
    if window_days and window_days > 0:
        window_clause = "WHERE created_at >= datetime('now', ?)"
        params.append(f"-{window_days} days")

    total_sql = f"SELECT COUNT(*) FROM retro_findings {window_clause}"
    total_findings = conn.execute(total_sql, params).fetchone()[0]

    summary_sql = f"SELECT summary FROM retro_findings {window_clause}"
    counter: Counter = Counter()
    for row in conn.execute(summary_sql, params):
        for term in _extract_terms(row["summary"]):
            counter[term] += 1

    themes = [
        {"theme": term, "count": count}
        for term, count in counter.items()
        if count >= min_recurrence
    ]
    themes.sort(key=lambda t: (-t["count"], t["theme"]))

    return {
        "window_days": window_days,
        "min_recurrence": min_recurrence,
        "total_findings": total_findings,
        "themes": themes,
    }


def main(argv: list) -> int:
    db_path = argv[0]
    # argv[1] is config_path — reserved for future use
    parser = argparse.ArgumentParser(
        prog="tusk retro-themes",
        description=(
            "Aggregate retro_findings by category (the 'theme') across a "
            "configurable look-back window. Output is pre-aggregated "
            "[{theme, count}] tuples — /retro never sees raw rows."
        ),
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=(
            "Look-back window for retro_findings.created_at. "
            f"Default {DEFAULT_WINDOW_DAYS}. 0 disables the filter."
        ),
    )
    parser.add_argument(
        "--min-recurrence",
        type=int,
        default=DEFAULT_MIN_RECURRENCE,
        help=(
            "Drop themes whose count is below this value. "
            f"Default {DEFAULT_MIN_RECURRENCE}. "
            "Use 3 to surface only recurring themes."
        ),
    )
    args = parser.parse_args(argv[2:])

    if args.window_days < 0:
        print("--window-days must be >= 0", file=sys.stderr)
        return 1
    if args.min_recurrence < 1:
        print("--min-recurrence must be >= 1", file=sys.stderr)
        return 1

    conn = get_connection(db_path)
    try:
        payload = fetch_themes(
            conn,
            window_days=args.window_days,
            min_recurrence=args.min_recurrence,
        )
        print(dumps(payload))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk retro-themes [--window-days N] [--min-recurrence N]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
