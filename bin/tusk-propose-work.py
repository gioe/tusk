#!/usr/bin/env python3
"""Aggregate origination signals into a ranked JSON array of candidate proposals.

The origination engine for the generative backlog path. When the backlog drains,
tusk proposes its own next work from signals it already records, so the operator
(or /loop) has something to act on instead of an empty queue. This command is
strictly READ-ONLY: it never inserts tasks, mutates rows, or touches the working
tree — it only reads existing signals and emits proposals.

Called by the tusk wrapper:
    tusk propose-work [--window-days N] [--limit N] [--no-todo-scan]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path

Flags:
    --window-days N   Look-back window for time-bounded signals (skill-patch
                      findings). Default 30. 0 disables the window filter.
    --limit N         Cap the emitted array at the N highest-scored proposals.
                      Default 0 (no cap).
    --no-todo-scan    Skip the repo TODO/FIXME filesystem scan (the only signal
                      that walks the filesystem rather than the DB).

Signal sources (each carries a distinct `source` label):
    skill_patch  — unconfirmed `skill-patch:<file>` retro_findings
                   (reuses tusk-retro-patches.py's fetch_patches()).
    next_steps   — unconsumed task_progress.next_steps handoff notes whose
                   originating task is still open (not Done).
    jot_category — recurring jot categories (>= a recurrence floor), the
                   highest-friction themes captured at the source.
    todo_scan    — TODO/FIXME/HACK/XXX comments found by a repo filesystem scan
                   (reuses tusk-init-scan-todos.py's scan()).
    cost_outlier — STRETCH: tools whose summed call_count across a task's
                   sessions exceeds the per-complexity outlier threshold.

Output JSON shape (single line, ranked highest-score first):
    [
        {
            "source": "skill_patch" | "next_steps" | "jot_category"
                      | "todo_scan" | "cost_outlier",
            "score": <float>,
            "title": "<short proposal headline>",
            "detail": "<supporting context>",
            "evidence": { ... source-specific provenance ... }
        },
        ...
    ]

An empty-signal environment returns `[]` and exits 0 (never an error).

Exit codes:
    0 — success (including the empty-signal case)
    1 — error (bad arguments, DB issue)
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # noqa: E402  # loads tusk-db-lib.py, tusk-json-lib.py, tusk-retro-patches.py, tusk-init-scan-todos.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_retro_patches = tusk_loader.load("tusk-retro-patches")
_todo_scan = tusk_loader.load("tusk-init-scan-todos")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection


DEFAULT_WINDOW_DAYS = 30

# Minimum number of times a jot category must recur to count as a theme worth
# proposing. One-off jots are noise; a repeated category is a friction pattern.
JOT_RECURRENCE_FLOOR = 2

# Per-complexity SUM(call_count) thresholds for the stretch cost_outlier source.
# Mirrors tusk-retro-signals.py's CALL_COUNT_THRESHOLDS so "outlier" means the
# same thing everywhere. None key applies to tasks with no complexity set.
CALL_COUNT_THRESHOLDS: dict = {
    "XS": 20, "S": 40, "M": 80, "L": 150, "XL": 300, None: 80,
}

# Base scores per source — the source's intrinsic priority before per-item
# scaling. Unconfirmed skill patches are the strongest signal (a behavior change
# we made and never validated); recurring friction and stale handoffs come next;
# TODO comments and cost outliers are the weakest (most speculative) signals.
SOURCE_BASE_SCORE: dict = {
    "skill_patch": 80.0,
    "next_steps": 60.0,
    "jot_category": 55.0,
    "cost_outlier": 40.0,
    "todo_scan": 30.0,
}

# Cap on free-text fields included in a proposal so the array stays compact.
_TITLE_MAX = 100
_DETAIL_MAX = 200


def _compact(text, limit):
    s = (text or "").strip().replace("\n", " ")
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def propose_skill_patches(conn, *, window_days):
    """Unconfirmed skill-patch findings → proposals to validate the patch.

    Reuses tusk-retro-patches.fetch_patches() with unconfirmed_only=True so this
    path stays in lockstep with `tusk retro-patches --unconfirmed`. Older patches
    score slightly higher (they have waited longest for validation)."""
    patches = _retro_patches.fetch_patches(
        conn, window_days=window_days, unconfirmed_only=True
    )
    proposals = []
    base = SOURCE_BASE_SCORE["skill_patch"]
    for p in patches:
        age = p.get("age_days") or 0
        # +1 per age-day, capped so a very old patch cannot dominate everything.
        score = base + min(float(age), 30.0)
        proposals.append({
            "source": "skill_patch",
            "score": round(score, 2),
            "title": _compact(
                f"Validate skill patch to {p['target_file']}", _TITLE_MAX
            ),
            "detail": _compact(
                f"Unconfirmed {p['action_taken']} from skill_run "
                f"{p['skill_run_id']} ({age}d old); file a "
                f"skill-patch-confirmed:{p['target_file']} once it holds.",
                _DETAIL_MAX,
            ),
            "evidence": {
                "finding_id": p["finding_id"],
                "skill_run_id": p["skill_run_id"],
                "task_id": p["task_id"],
                "target_file": p["target_file"],
                "age_days": age,
            },
        })
    return proposals


def propose_next_steps(conn):
    """Unconsumed task_progress.next_steps on still-open tasks → proposals.

    A next_steps note on a task that is NOT Done is an unfinished thread — the
    author handed off work that the backlog may have lost track of. We take the
    most recent non-empty note per open task (older notes are usually subsumed
    by newer ones on the same task)."""
    rows = conn.execute(
        """
        SELECT tp.task_id      AS task_id,
               t.summary        AS summary,
               t.status         AS status,
               tp.next_steps    AS next_steps,
               MAX(tp.created_at) AS created_at
          FROM task_progress tp
          JOIN tasks t ON t.id = tp.task_id
         WHERE tp.next_steps IS NOT NULL
           AND TRIM(tp.next_steps) <> ''
           AND t.status <> 'Done'
         GROUP BY tp.task_id
         ORDER BY created_at DESC, tp.task_id DESC
        """
    ).fetchall()
    proposals = []
    base = SOURCE_BASE_SCORE["next_steps"]
    for r in rows:
        proposals.append({
            "source": "next_steps",
            "score": round(base, 2),
            "title": _compact(
                f"Resume TASK-{r['task_id']}: {r['summary']}", _TITLE_MAX
            ),
            "detail": _compact(r["next_steps"], _DETAIL_MAX),
            "evidence": {
                "task_id": r["task_id"],
                "status": r["status"],
                "created_at": r["created_at"],
            },
        })
    return proposals


def propose_jot_categories(conn, *, recurrence_floor=JOT_RECURRENCE_FLOOR):
    """Recurring jot categories (count >= floor) → friction-theme proposals.

    A jot category that keeps recurring is a friction pattern the operator hits
    repeatedly. Score scales with recurrence: the more often it shows up, the
    stronger the case for addressing it."""
    rows = conn.execute(
        """
        SELECT category,
               COUNT(*) AS cnt,
               (SELECT note FROM jots j2
                 WHERE j2.category IS jots.category
                 ORDER BY j2.created_at DESC, j2.id DESC
                 LIMIT 1) AS sample_note
          FROM jots
         GROUP BY category
        HAVING COUNT(*) >= ?
         ORDER BY cnt DESC, category
        """,
        (recurrence_floor,),
    ).fetchall()
    proposals = []
    base = SOURCE_BASE_SCORE["jot_category"]
    for r in rows:
        cnt = int(r["cnt"])
        # +5 per occurrence beyond the floor.
        score = base + (cnt - recurrence_floor) * 5.0
        proposals.append({
            "source": "jot_category",
            "score": round(score, 2),
            "title": _compact(
                f"Address recurring '{r['category']}' friction ({cnt}x)",
                _TITLE_MAX,
            ),
            "detail": _compact(
                f"{cnt} jots in category '{r['category']}'. "
                f"Latest: {r['sample_note']}",
                _DETAIL_MAX,
            ),
            "evidence": {
                "category": r["category"],
                "count": cnt,
            },
        })
    return proposals


def propose_todo_scan(repo_root):
    """Repo TODO/FIXME/HACK/XXX comments → proposals.

    Reuses tusk-init-scan-todos.scan() so the keyword set, comment-context
    matching, and false-positive filtering stay identical to /tusk-init's TODO
    seeding. High-priority keywords (FIXME/HACK) score above plain TODOs."""
    if not repo_root or not os.path.isdir(repo_root):
        return []
    try:
        matches = _todo_scan.scan(repo_root)
    except OSError:
        return []
    proposals = []
    base = SOURCE_BASE_SCORE["todo_scan"]
    for m in matches:
        bump = 15.0 if m.get("priority") == "High" else 0.0
        proposals.append({
            "source": "todo_scan",
            "score": round(base + bump, 2),
            "title": _compact(f"{m['keyword']}: {m['text']}", _TITLE_MAX),
            "detail": _compact(
                f"{m['file']}:{m['line']} — {m['text']}", _DETAIL_MAX
            ),
            "evidence": {
                "file": m["file"],
                "line": m["line"],
                "keyword": m["keyword"],
                "priority": m["priority"],
            },
        })
    return proposals


def propose_cost_outliers(conn):
    """STRETCH: tools whose summed call_count for a task exceeds the
    per-complexity outlier threshold → "investigate cost" proposals.

    Mirrors tusk-retro-signals.fetch_tool_call_outliers but ranges over every
    task rather than one. Score scales with how far over threshold the tool ran.
    Degrades to [] if the cost tables are absent (older DBs)."""
    try:
        rows = conn.execute(
            """
            SELECT ts.task_id        AS task_id,
                   t.complexity      AS complexity,
                   tcs.tool_name     AS tool_name,
                   SUM(tcs.call_count) AS total_calls,
                   SUM(tcs.total_cost) AS total_cost
              FROM tool_call_stats tcs
              JOIN task_sessions ts ON ts.id = tcs.session_id
              JOIN tasks t ON t.id = ts.task_id
             WHERE tcs.session_id IS NOT NULL
             GROUP BY ts.task_id, tcs.tool_name
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    proposals = []
    base = SOURCE_BASE_SCORE["cost_outlier"]
    for r in rows:
        complexity = r["complexity"]
        threshold = CALL_COUNT_THRESHOLDS.get(
            complexity, CALL_COUNT_THRESHOLDS[None]
        )
        total_calls = int(r["total_calls"] or 0)
        if total_calls < threshold:
            continue
        over = total_calls - threshold
        score = base + min(float(over), 30.0)
        proposals.append({
            "source": "cost_outlier",
            "score": round(score, 2),
            "title": _compact(
                f"Investigate {r['tool_name']} usage on TASK-{r['task_id']} "
                f"({total_calls} calls)",
                _TITLE_MAX,
            ),
            "detail": _compact(
                f"{total_calls} {r['tool_name']} calls exceed the "
                f"{complexity or 'default'} threshold of {threshold} "
                f"(${float(r['total_cost'] or 0.0):.2f}).",
                _DETAIL_MAX,
            ),
            "evidence": {
                "task_id": r["task_id"],
                "tool_name": r["tool_name"],
                "call_count": total_calls,
                "threshold": threshold,
                "complexity": complexity,
            },
        })
    return proposals


def build_proposals(
    conn,
    repo_root,
    *,
    window_days=DEFAULT_WINDOW_DAYS,
    include_todo_scan=True,
    include_cost_outliers=True,
    limit=0,
):
    """Aggregate every signal source into one ranked list, highest score first.

    Each source contributes independently; a failure in one source must not
    sink the others, so DB-backed sources that touch optional tables degrade to
    [] on their own. Ties break on source base score then title for a stable,
    deterministic order."""
    proposals = []
    proposals.extend(propose_skill_patches(conn, window_days=window_days))
    proposals.extend(propose_next_steps(conn))
    proposals.extend(propose_jot_categories(conn))
    if include_todo_scan:
        proposals.extend(propose_todo_scan(repo_root))
    if include_cost_outliers:
        proposals.extend(propose_cost_outliers(conn))

    proposals.sort(
        key=lambda p: (-p["score"], p["source"], p["title"])
    )
    if limit and limit > 0:
        proposals = proposals[:limit]
    return proposals


def _repo_root_from_db_path(db_path):
    """The DB lives at <repo_root>/tusk/tasks.db → repo_root is two dirs up.

    Falls back to None if the layout doesn't match (the TODO scan then no-ops)."""
    db_dir = os.path.dirname(os.path.abspath(db_path))
    if os.path.basename(db_dir) == "tusk":
        return os.path.dirname(db_dir)
    return None


def main(argv):
    db_path = argv[0]
    # argv[1] is config_path — reserved for future use
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        prog="tusk propose-work",
        description=(
            "Aggregate origination signals (unconfirmed skill patches, "
            "unconsumed next_steps, recurring jot categories, and a repo "
            "TODO/FIXME scan) into a ranked JSON array of candidate proposals. "
            "Read-only — never inserts tasks."
        ),
    )
    parser.add_argument(
        "--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
        help=(
            "Look-back window for time-bounded signals (skill-patch findings). "
            f"Default {DEFAULT_WINDOW_DAYS}. 0 disables the filter."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Cap the emitted array at the N highest-scored proposals (0 = no cap).",
    )
    parser.add_argument(
        "--no-todo-scan", action="store_true",
        help="Skip the repo TODO/FIXME filesystem scan.",
    )
    parser.add_argument(
        "--no-cost-outliers", action="store_true",
        help="Skip the stretch cost-outlier source.",
    )
    args = parser.parse_args(argv[2:])

    if args.window_days < 0:
        print("--window-days must be >= 0", file=sys.stderr)
        return 1
    if args.limit < 0:
        print("--limit must be >= 0", file=sys.stderr)
        return 1

    repo_root = _repo_root_from_db_path(db_path)

    try:
        conn = get_connection(db_path)
    except Exception as e:  # noqa: BLE001
        print(
            f"tusk propose-work: failed to open database '{db_path}': "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1
    try:
        proposals = build_proposals(
            conn,
            repo_root,
            window_days=args.window_days,
            include_todo_scan=not args.no_todo_scan,
            include_cost_outliers=not args.no_cost_outliers,
            limit=args.limit,
        )
        print(dumps(proposals))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print(
            "Use: tusk propose-work [--window-days N] [--limit N] [--no-todo-scan]",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
