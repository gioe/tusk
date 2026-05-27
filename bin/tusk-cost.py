#!/usr/bin/env python3
"""Cumulative project cost rollup.

Called by the tusk wrapper:
    tusk cost [--format json|text]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (unused; kept for dispatch consistency)
    sys.argv[3:] — optional flags
"""

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # noqa: E402

_db_lib = tusk_loader.load("tusk-db-lib")
get_connection = _db_lib.get_connection


def _money(value: float | None) -> float:
    return round(float(value or 0.0), 4)


def _has_cost(value) -> bool:
    return value is not None


def _is_shadowed_tusk_skill_run(sr: sqlite3.Row, sessions: list[sqlite3.Row]) -> bool:
    """Return true when a tusk skill run is already represented by a session.

    ``/tusk`` opens one task_session and one ``skill_runs.skill_name='tusk'``
    row for the same task. If the matching session has cost data, the session
    is the authoritative project-level cost row and the tusk skill run is a
    shadow that would double-count the same model calls.
    """
    if sr["skill_name"] != "tusk" or sr["task_id"] is None:
        return False
    if not _has_cost(sr["cost_dollars"]):
        return False

    sr_start = sr["started_at"] or ""
    for session in sessions:
        if session["task_id"] != sr["task_id"]:
            continue
        if not _has_cost(session["cost_dollars"]):
            continue
        session_start = session["started_at"] or ""
        if sr_start[:16] == session_start[:16]:
            return True
    return False


def build_cost_summary(conn: sqlite3.Connection) -> dict:
    sessions = conn.execute(
        "SELECT id, task_id, started_at, ended_at, cost_dollars FROM task_sessions"
    ).fetchall()
    skill_runs = conn.execute(
        "SELECT id, skill_name, task_id, started_at, ended_at, cost_dollars FROM skill_runs"
    ).fetchall()

    session_cost = sum(float(r["cost_dollars"]) for r in sessions if _has_cost(r["cost_dollars"]))
    session_missing = sum(1 for r in sessions if not _has_cost(r["cost_dollars"]))

    included_skill_cost = 0.0
    included_skill_count = 0
    deduped_tusk_cost = 0.0
    deduped_tusk_count = 0
    skill_missing = 0

    for sr in skill_runs:
        if not _has_cost(sr["cost_dollars"]):
            skill_missing += 1
            continue
        if _is_shadowed_tusk_skill_run(sr, sessions):
            deduped_tusk_cost += float(sr["cost_dollars"])
            deduped_tusk_count += 1
            continue
        included_skill_cost += float(sr["cost_dollars"])
        included_skill_count += 1

    total = session_cost + included_skill_cost
    return {
        "total_cost_dollars": _money(total),
        "task_session_cost_dollars": _money(session_cost),
        "additional_skill_run_cost_dollars": _money(included_skill_cost),
        "deduped_tusk_skill_run_cost_dollars": _money(deduped_tusk_cost),
        "task_sessions": {
            "total": len(sessions),
            "costed": len(sessions) - session_missing,
            "missing_cost": session_missing,
        },
        "skill_runs": {
            "total": len(skill_runs),
            "included_costed": included_skill_count,
            "deduped_tusk_shadows": deduped_tusk_count,
            "missing_cost": skill_missing,
        },
        "coverage": {
            "task_sessions_missing_cost": session_missing,
            "skill_runs_missing_cost": skill_missing,
        },
        "method": (
            "sum task_sessions.cost_dollars plus non-shadow skill_runs.cost_dollars; "
            "dedupe cost-bearing tusk skill_runs that start in the same minute as a "
            "cost-bearing task_session for the same task"
        ),
    }


def _render_text(summary: dict) -> str:
    lines = [
        f"Total project cost: ${summary['total_cost_dollars']:.4f}",
        f"  Task sessions:    ${summary['task_session_cost_dollars']:.4f}",
        f"  Extra skill runs: ${summary['additional_skill_run_cost_dollars']:.4f}",
        f"  Deduped tusk runs: ${summary['deduped_tusk_skill_run_cost_dollars']:.4f}",
        "",
        "Coverage:",
        f"  task_sessions: {summary['task_sessions']['costed']} costed / "
        f"{summary['task_sessions']['total']} total "
        f"({summary['task_sessions']['missing_cost']} missing cost)",
        f"  skill_runs:    {summary['skill_runs']['included_costed']} included costed / "
        f"{summary['skill_runs']['total']} total "
        f"({summary['skill_runs']['deduped_tusk_shadows']} deduped tusk shadows, "
        f"{summary['skill_runs']['missing_cost']} missing cost)",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tusk cost",
        usage="tusk cost [--format json|text]",
        description="Report cumulative project cost.",
    )
    parser.add_argument("db_path", help=argparse.SUPPRESS)
    parser.add_argument("config_path", help=argparse.SUPPRESS)
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="text",
        help="Output format (default: text).",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    conn = get_connection(args.db_path)
    try:
        summary = build_cost_summary(conn)
    finally:
        conn.close()

    if args.format == "json":
        print(json.dumps(summary, sort_keys=True))
    else:
        print(_render_text(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
