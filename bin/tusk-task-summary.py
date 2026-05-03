#!/usr/bin/env python3
"""End-of-run task summary: identity, cost, duration, diff stats, and criteria counts.

Called by the tusk wrapper at the end of every /tusk run (Step 12 of skills/tusk/SKILL.md),
after tusk merge / tusk abandon and before handing off to /retro, so the user sees a
canonical "here's the task that just finished" block before retrospective findings.

Invocation:
    tusk task-summary <task_id> [--format json|markdown]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (unused — kept for dispatch consistency)
    sys.argv[3:] — task_id + optional flags

Output shape (JSON, default):
    {
        "task_id": N,
        "prefixed_id": "TASK-N",
        "summary": "...",
        "status": "Done",
        "closed_reason": "completed" | "wont_do" | "duplicate" | "expired" | null,
        "cost": {"total": 0.1234, "skill_run_count": N},
        "baseline_comparison": {
            "bucket": "M" | null,
            "median_cost": 0.0612 | null,
            "n": N,
            "ratio": 2.5 | null,
            "threshold": N,
            "status": "compared" | "pending" | "no_complexity" | "no_peers"
        },
        "tokens": {"tokens_in": N, "tokens_out": N, "request_count": N},
        "duration": {
            "wall_seconds": N | null,
            "active_seconds": N,
            "started_at": "..." | null,
            "closed_at": "..." | null,
            "session_count": N
        },
        "diff": {
            "commits": N,
            "files_changed": N,
            "lines_added": N,
            "lines_removed": N
        },
        "criteria": {
            "total": N,
            "manual": N,
            "automated": N,
            "skip_notes": N,
            "deferred": N,
            "deferred_details": [
                {"id": N, "criterion": "...", "deferred_reason": "..."},
                ...
            ]
        },
        "review_passes": N,
        "reopen_count": N
    }

With --format markdown, the same data is rendered as a user-facing block.

Diff stats are derived from `git log --grep="[TASK-<id>]"` — commits that don't
reference the task ID are excluded, preventing cross-task pollution on shared
branches. The query is also scoped with `--since=<tasks.started_at>` so commits
from an earlier incarnation of the same numeric ID (e.g., after a fresh DB init
where IDs reset) are excluded. If the task was abandoned (no commits), all diff
fields are 0.

Exit codes:
    0 — success
    1 — error (bad arguments, task not found, DB issue)
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # noqa: E402 — loads tusk-db-lib.py, tusk-json-lib.py, tusk-pricing-lib.py, tusk-git-helpers.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_pricing_lib = tusk_loader.load("tusk-pricing-lib")
_git_helpers = tusk_loader.load("tusk-git-helpers")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
task_grep_arg = _git_helpers.task_grep_arg


def _resolve_task_id(raw: str) -> int:
    """Accept '5' or 'TASK-5' → 5. Raises ValueError on junk."""
    return int(re.sub(r"^TASK-", "", raw, flags=re.IGNORECASE))


def fetch_identity(conn: sqlite3.Connection, task_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, summary, status, closed_reason, complexity, started_at, closed_at "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "summary": row["summary"],
        "status": row["status"],
        "closed_reason": row["closed_reason"],
        "complexity": row["complexity"],
        "started_at": row["started_at"],
        "closed_at": row["closed_at"],
    }


def fetch_cost(conn: sqlite3.Connection, task_id: int) -> dict:
    """SUM(cost_dollars) across every skill_runs row attributed to the task."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_dollars), 0.0) AS total, COUNT(*) AS cnt "
        "FROM skill_runs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return {
        "total": round(float(row["total"] or 0.0), 4),
        "skill_run_count": int(row["cnt"] or 0),
    }


def fetch_baseline_comparison(
    conn: sqlite3.Connection,
    task_id: int,
    complexity: str | None,
    current_cost: float,
    threshold: int,
) -> dict:
    """Median cost of completed peers in the same complexity bucket.

    Median is robust to outlier sessions (a single runaway agent run won't skew
    the baseline). Peers are restricted to status='Done' AND closed_reason='completed'
    so wont_do/duplicate stubs don't dilute the bucket. Per-task cost is summed
    from skill_runs to match fetch_cost; tasks with zero recorded cost are excluded
    via HAVING so empty/orphaned rows don't drag the median to zero.

    Status values:
        no_complexity — current task has no complexity assigned (cannot bucket)
        no_peers      — bucket has zero qualifying peers (first task in bucket)
        pending       — bucket has 1..threshold-1 peers (sample too small to compare)
        compared      — bucket has >= threshold peers; ratio is populated
    """
    if not complexity:
        return {
            "bucket": None,
            "median_cost": None,
            "n": 0,
            "ratio": None,
            "threshold": threshold,
            "status": "no_complexity",
        }

    rows = conn.execute(
        "SELECT COALESCE(SUM(sr.cost_dollars), 0.0) AS total "
        "FROM tasks t "
        "LEFT JOIN skill_runs sr ON sr.task_id = t.id "
        "WHERE t.status = 'Done' "
        "  AND t.closed_reason = 'completed' "
        "  AND t.complexity = ? "
        "  AND t.id <> ? "
        "GROUP BY t.id "
        "HAVING total > 0",
        (complexity, task_id),
    ).fetchall()

    peer_costs = sorted(float(r["total"]) for r in rows)
    n = len(peer_costs)

    if n == 0:
        return {
            "bucket": complexity,
            "median_cost": None,
            "n": 0,
            "ratio": None,
            "threshold": threshold,
            "status": "no_peers",
        }

    if n % 2 == 1:
        median = peer_costs[n // 2]
    else:
        median = (peer_costs[n // 2 - 1] + peer_costs[n // 2]) / 2

    if n < threshold:
        return {
            "bucket": complexity,
            "median_cost": round(median, 4),
            "n": n,
            "ratio": None,
            "threshold": threshold,
            "status": "pending",
        }

    # Suppress the multiplier for in-progress / not-yet-started tasks: a zero
    # current_cost would otherwise render as "0.0x baseline", which reads as
    # "this task was cheap" rather than "this task hasn't accumulated cost yet".
    # The bucket median + n still ship in compared status — useful context even
    # before the run finishes.
    ratio = current_cost / median if (median > 0 and current_cost > 0) else None
    return {
        "bucket": complexity,
        "median_cost": round(median, 4),
        "n": n,
        "ratio": round(ratio, 2) if ratio is not None else None,
        "threshold": threshold,
        "status": "compared",
    }


def fetch_tokens(conn: sqlite3.Connection, task_id: int) -> dict:
    """Sum tokens_in, tokens_out, request_count across skill_runs for the task."""
    row = conn.execute(
        "SELECT COALESCE(SUM(tokens_in), 0) AS tin, "
        "       COALESCE(SUM(tokens_out), 0) AS tout, "
        "       COALESCE(SUM(request_count), 0) AS req "
        "FROM skill_runs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return {
        "tokens_in": int(row["tin"] or 0),
        "tokens_out": int(row["tout"] or 0),
        "request_count": int(row["req"] or 0),
    }


def fetch_duration(conn: sqlite3.Connection, task_id: int, identity: dict) -> dict:
    """Wall time = earliest session start → task closed_at; active = SUM(session.duration_seconds)."""
    row = conn.execute(
        "SELECT COUNT(*) AS cnt, "
        "       MIN(started_at) AS first_start, "
        "       COALESCE(SUM(duration_seconds), 0) AS active "
        "FROM task_sessions WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    first_start = row["first_start"]
    closed_at = identity["closed_at"]
    wall = None
    if first_start and closed_at:
        try:
            start_dt = _pricing_lib.parse_sqlite_timestamp(first_start)
            end_dt = _pricing_lib.parse_sqlite_timestamp(closed_at)
            wall = int((end_dt - start_dt).total_seconds())
            if wall < 0:
                wall = 0
        except (ValueError, TypeError):
            wall = None
    return {
        "wall_seconds": wall,
        "active_seconds": int(row["active"] or 0),
        "started_at": first_start,
        "closed_at": closed_at,
        "session_count": int(row["cnt"] or 0),
    }


def fetch_diff(task_id: int, repo_root: str, since: str | None = None) -> dict:
    """Parse `git log --grep` output to collect commit count, unique files, and line deltas.

    `--all` scans every ref so post-merge commits (now on the default branch)
    are still found. The `[TASK-<id>]` grep filter excludes commits for other
    tasks that happen to sit on the same branch history. When `since` is
    provided (typically `tasks.started_at`), `--since=<since> UTC` is appended
    so commits authored before this task's lifetime — e.g. an earlier
    incarnation of the same numeric ID after a fresh DB init — are excluded.
    The "UTC" suffix anchors the SQLite-stored UTC timestamp against git's
    local-time interpretation of `--since`.
    """
    zero = {"commits": 0, "files_changed": 0, "lines_added": 0, "lines_removed": 0}
    cmd = [
        "git", "log", "--all",
        task_grep_arg(task_id),
        "--numstat",
        "--format=__COMMIT__ %H",
    ]
    if since:
        cmd.append(f"--since={since} UTC")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=repo_root,
        )
    except (OSError, subprocess.SubprocessError):
        return zero
    if result.returncode != 0:
        return zero

    commits: set[str] = set()
    files: set[str] = set()
    added = 0
    removed = 0
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        if line.startswith("__COMMIT__ "):
            commits.add(line.split(" ", 1)[1].strip())
            continue
        # numstat row: "<added>\t<removed>\t<path>" (or "- -" for binary files)
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a, r, path = parts[0], parts[1], parts[2]
        files.add(path)
        try:
            added += int(a)
        except ValueError:
            pass  # binary: "-"
        try:
            removed += int(r)
        except ValueError:
            pass
    return {
        "commits": len(commits),
        "files_changed": len(files),
        "lines_added": added,
        "lines_removed": removed,
    }


def fetch_criteria(conn: sqlite3.Connection, task_id: int) -> dict:
    """Counts by kind and skip signal, plus per-criterion deferred details.

    `skip_notes` captures criteria closed with `--skip-verify --note "..."` (the
    note lands in `acceptance_criteria.skip_note`). `deferred` captures the
    `tusk criteria skip --reason` path which sets `is_deferred=1`. Together they
    cover every "acknowledged gap at close" signal the schema records.

    `deferred_details` is a per-row list (id, criterion, deferred_reason) so the
    markdown rollup and downstream consumers can distinguish *why* each
    criterion was deferred — chain orchestration vs not-applicable vs other
    rationales — instead of seeing only an aggregate count.
    """
    row = conn.execute(
        "SELECT "
        "  COUNT(*) AS total, "
        "  SUM(CASE WHEN criterion_type = 'manual' THEN 1 ELSE 0 END) AS manual, "
        "  SUM(CASE WHEN criterion_type IN ('code', 'test', 'file') THEN 1 ELSE 0 END) AS automated, "
        "  SUM(CASE WHEN skip_note IS NOT NULL AND TRIM(skip_note) <> '' THEN 1 ELSE 0 END) AS skip_notes, "
        "  SUM(CASE WHEN is_deferred = 1 THEN 1 ELSE 0 END) AS deferred "
        "FROM acceptance_criteria WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    deferred_rows = conn.execute(
        "SELECT id, criterion, deferred_reason FROM acceptance_criteria "
        "WHERE task_id = ? AND is_deferred = 1 ORDER BY id",
        (task_id,),
    ).fetchall()
    return {
        "total": int(row["total"] or 0),
        "manual": int(row["manual"] or 0),
        "automated": int(row["automated"] or 0),
        "skip_notes": int(row["skip_notes"] or 0),
        "deferred": int(row["deferred"] or 0),
        "deferred_details": [
            {
                "id": int(r["id"]),
                "criterion": r["criterion"],
                "deferred_reason": r["deferred_reason"],
            }
            for r in deferred_rows
        ],
    }


def fetch_review_passes(conn: sqlite3.Connection, task_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM code_reviews WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return int(row["cnt"] or 0)


def fetch_reopen_count(conn: sqlite3.Connection, task_id: int) -> int:
    """Transitions back into 'To Do' — covers both mid-task rework and post-Done reopens."""
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM task_status_transitions "
        "WHERE task_id = ? AND to_status = 'To Do'",
        (task_id,),
    ).fetchone()
    return int(row["cnt"] or 0)


def build_summary(
    conn: sqlite3.Connection,
    task_id: int,
    repo_root: str,
    baseline_threshold: int = 10,
) -> dict | None:
    identity = fetch_identity(conn, task_id)
    if identity is None:
        return None
    cost = fetch_cost(conn, task_id)
    return {
        "task_id": identity["id"],
        "prefixed_id": f"TASK-{identity['id']}",
        "summary": identity["summary"],
        "status": identity["status"],
        "closed_reason": identity["closed_reason"],
        "cost": cost,
        "baseline_comparison": fetch_baseline_comparison(
            conn, task_id, identity["complexity"], cost["total"], baseline_threshold
        ),
        "tokens": fetch_tokens(conn, task_id),
        "duration": fetch_duration(conn, task_id, identity),
        "diff": fetch_diff(task_id, repo_root, since=identity["started_at"]),
        "criteria": fetch_criteria(conn, task_id),
        "review_passes": fetch_review_passes(conn, task_id),
        "reopen_count": fetch_reopen_count(conn, task_id),
    }


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    mins, secs = divmod(seconds, 60)
    if mins < 60:
        return f"{mins}m {secs}s" if secs else f"{mins}m"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m" if mins else f"{hours}h"


def _render_cost_line(cost: dict, baseline: dict) -> str:
    plural = "s" if cost["skill_run_count"] != 1 else ""
    base = (
        f"- **Cost:** ${cost['total']:.4f} across "
        f"{cost['skill_run_count']} skill run{plural}"
    )
    status = baseline.get("status")
    if status == "compared":
        bucket_info = (
            f"{baseline['bucket']} median: ${baseline['median_cost']:.4f}, "
            f"n={baseline['n']}"
        )
        if baseline.get("ratio") is None:
            # Zero-cost current task: show the bucket context but skip the multiplier
            return f"{base} ({bucket_info})"
        return f"{base} — {baseline['ratio']:.1f}x baseline ({bucket_info})"
    if status in ("pending", "no_peers"):
        return (
            f"{base} (baseline pending — {baseline['bucket']} bucket has "
            f"{baseline['n']}/{baseline['threshold']} closed tasks)"
        )
    return base


def render_markdown(data: dict) -> str:
    closed = data["closed_reason"] or "—"
    cost = data["cost"]
    baseline = data["baseline_comparison"]
    dur = data["duration"]
    diff = data["diff"]
    crit = data["criteria"]

    lines = [
        f"## {data['prefixed_id']} — {data['summary']} ({data['status']} / {closed})",
        "",
        _render_cost_line(cost, baseline),
        f"- **Duration:** {_format_duration(dur['wall_seconds'])} wall / "
        f"{_format_duration(dur['active_seconds'])} active "
        f"({dur['session_count']} session{'s' if dur['session_count'] != 1 else ''})",
        f"- **Changes:** {diff['files_changed']} file"
        f"{'s' if diff['files_changed'] != 1 else ''} · "
        f"+{diff['lines_added']} / −{diff['lines_removed']} lines · "
        f"{diff['commits']} commit{'s' if diff['commits'] != 1 else ''}",
        f"- **Criteria:** {crit['total']} total "
        f"({crit['manual']} manual, {crit['automated']} automated)"
        + (
            f" · {crit['skip_notes']} skip-verify"
            if crit["skip_notes"]
            else ""
        )
        + (f" · {crit['deferred']} deferred" if crit["deferred"] else ""),
    ]
    for d in crit.get("deferred_details", []):
        reason = d.get("deferred_reason") or "no reason given"
        lines.append(f"  - _Deferred #{d['id']} ({reason}):_ {d['criterion']}")
    lines.append(
        f"- **Review passes:** {data['review_passes']}"
        + (f" · **Reopened:** {data['reopen_count']}×" if data["reopen_count"] else "")
    )
    return "\n".join(lines)


def _load_baseline_threshold(config_path: str) -> int:
    """Read baseline_min_sample_size from config; default to 10 if missing/invalid/unreadable."""
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        val = cfg.get("baseline_min_sample_size", 10)
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            return val
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return 10


def main(argv: list) -> int:
    db_path = argv[0]
    config_path = argv[1]
    parser = argparse.ArgumentParser(
        prog="tusk task-summary",
        description="Emit an end-of-run summary for a task (identity, cost, duration, diff, criteria).",
    )
    parser.add_argument("task_id", help="Task ID (integer or TASK-NNN prefix form)")
    parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="json",
        help="Output format (default: json).",
    )
    args = parser.parse_args(argv[2:])

    try:
        task_id = _resolve_task_id(args.task_id)
    except ValueError:
        print(f"Invalid task ID: {args.task_id}", file=sys.stderr)
        return 1

    # repo_root is two levels up from the DB: tusk/tasks.db → tusk/ → repo_root
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))
    threshold = _load_baseline_threshold(config_path)

    conn = get_connection(db_path)
    try:
        data = build_summary(conn, task_id, repo_root, baseline_threshold=threshold)
        if data is None:
            print(f"Task {task_id} not found", file=sys.stderr)
            return 1
        if args.format == "markdown":
            print(render_markdown(data))
        else:
            print(dumps(data))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-summary <task_id> [--format json|markdown]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
