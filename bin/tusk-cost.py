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


UNAVAILABLE_STATUSES = {
    "transcript_missing",
    "no_usage",
    "model_missing",
    "unpriced_model",
}


def _row_value(row: sqlite3.Row, column: str):
    return row[column] if column in row.keys() else None


def _is_legacy_cancelled_skill_run(row: sqlite3.Row) -> bool:
    return bool(
        "skill_name" in row.keys()
        and row["ended_at"]
        and row["telemetry_status"] is None
        and row["cost_dollars"] == 0
        and _row_value(row, "tokens_in") == 0
        and _row_value(row, "tokens_out") == 0
        and _row_value(row, "request_count") == 0
        and _row_value(row, "model") == ""
        and _row_value(row, "metadata") is None
    )


def _has_positive_usage(row: sqlite3.Row) -> bool:
    return any(
        (_row_value(row, column) or 0) > 0
        for column in ("tokens_in", "tokens_out", "request_count")
    )


def _accounting_state(row: sqlite3.Row) -> str:
    """Classify a telemetry row as known, unavailable, or excluded."""
    status = row["telemetry_status"]
    if status in {"pending", "cancelled"} or _is_legacy_cancelled_skill_run(row):
        return "excluded"
    if status in UNAVAILABLE_STATUSES:
        return "unavailable"
    if status is not None:
        return "known" if _has_cost(row["cost_dollars"]) else "excluded"

    if not row["ended_at"]:
        return "known" if _has_cost(row["cost_dollars"]) else "excluded"
    if row["cost_dollars"] is None:
        return "unavailable"
    if (
        "skill_name" in row.keys()
        and row["cost_dollars"] == 0
        and not _has_positive_usage(row)
    ):
        return "unavailable"
    return "known"


def _fetch_rows(conn: sqlite3.Connection, table: str, columns: tuple[str, ...]):
    available = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    selected = [column if column in available else f"NULL AS {column}" for column in columns]
    return conn.execute(f"SELECT {', '.join(selected)} FROM {table}").fetchall()


def _is_shadowed_tusk_skill_run(sr: sqlite3.Row, sessions: list[sqlite3.Row]) -> bool:
    """Return true when a tusk skill run is already represented by a session.

    ``/tusk`` opens one task_session and one ``skill_runs.skill_name='tusk'``
    row for the same task. If the matching session has cost data, the session
    is the authoritative project-level cost row and the tusk skill run is a
    shadow that would double-count the same model calls.
    """
    if sr["skill_name"] != "tusk" or sr["task_id"] is None:
        return False
    sr_state = _accounting_state(sr)
    if sr_state == "excluded":
        return False

    sr_start = sr["started_at"] or ""
    for session in sessions:
        if session["task_id"] != sr["task_id"]:
            continue
        session_state = _accounting_state(session)
        if session_state == "excluded":
            continue
        if sr_state == "known" and session_state != "known":
            continue
        session_start = session["started_at"] or ""
        if sr_start[:16] == session_start[:16]:
            return True
    return False


def build_cost_summary(conn: sqlite3.Connection) -> dict:
    common_columns = (
        "id", "task_id", "started_at", "ended_at", "cost_dollars",
        "telemetry_status", "tokens_in", "tokens_out", "request_count",
        "model", "metadata",
    )
    sessions = _fetch_rows(conn, "task_sessions", common_columns)
    skill_runs = _fetch_rows(conn, "skill_runs", ("skill_name",) + common_columns)

    session_cost = 0.0
    session_costed = 0
    session_unavailable = 0
    session_excluded = 0
    for session in sessions:
        state = _accounting_state(session)
        if state == "known":
            session_cost += float(session["cost_dollars"])
            session_costed += 1
        elif state == "unavailable":
            session_unavailable += 1
        else:
            session_excluded += 1

    included_skill_cost = 0.0
    included_skill_count = 0
    deduped_tusk_cost = 0.0
    deduped_tusk_count = 0
    deduped_tusk_unavailable = 0
    skill_unavailable = 0
    skill_excluded = 0

    for sr in skill_runs:
        state = _accounting_state(sr)
        if state == "excluded":
            skill_excluded += 1
            continue
        if _is_shadowed_tusk_skill_run(sr, sessions):
            deduped_tusk_count += 1
            if state == "known":
                deduped_tusk_cost += float(sr["cost_dollars"])
            else:
                deduped_tusk_unavailable += 1
            continue
        if state == "known":
            included_skill_cost += float(sr["cost_dollars"])
            included_skill_count += 1
        else:
            skill_unavailable += 1

    total = session_cost + included_skill_cost
    known_count = session_costed + included_skill_count
    unavailable_count = session_unavailable + skill_unavailable
    if known_count and unavailable_count:
        cost_status = "partial"
    elif known_count:
        cost_status = "complete"
    elif unavailable_count:
        cost_status = "unavailable"
    else:
        cost_status = "no_data"
    return {
        "total_cost_dollars": _money(total),
        "known_subtotal_dollars": _money(total),
        "cost_status": cost_status,
        "unavailable_completed_windows": unavailable_count,
        "task_session_cost_dollars": _money(session_cost),
        "additional_skill_run_cost_dollars": _money(included_skill_cost),
        "deduped_tusk_skill_run_cost_dollars": _money(deduped_tusk_cost),
        "task_sessions": {
            "total": len(sessions),
            "costed": session_costed,
            "unavailable": session_unavailable,
            "excluded": session_excluded,
            "missing_cost": session_unavailable,
        },
        "skill_runs": {
            "total": len(skill_runs),
            "included_costed": included_skill_count,
            "deduped_tusk_shadows": deduped_tusk_count,
            "deduped_tusk_unavailable": deduped_tusk_unavailable,
            "unavailable": skill_unavailable,
            "excluded": skill_excluded,
            "missing_cost": skill_unavailable,
        },
        "coverage": {
            "task_sessions_missing_cost": session_unavailable,
            "skill_runs_missing_cost": skill_unavailable,
            "task_sessions_excluded": session_excluded,
            "skill_runs_excluded": skill_excluded,
        },
        "method": (
            "sum known task_sessions.cost_dollars plus known non-shadow "
            "skill_runs.cost_dollars; report completed unavailable windows separately; "
            "exclude pending and cancelled rows; dedupe accounted tusk skill runs that "
            "start in the same minute as an authoritative task_session for the same task"
        ),
    }


def _render_text(summary: dict) -> str:
    status = summary["cost_status"]
    unavailable = summary["unavailable_completed_windows"]
    if status == "complete":
        headline = f"Total project cost: ${summary['total_cost_dollars']:.4f}"
    elif status == "partial":
        plural = "s" if unavailable != 1 else ""
        headline = (
            f"Known project cost subtotal: ${summary['known_subtotal_dollars']:.4f} "
            f"({unavailable} completed window{plural} unavailable)"
        )
    elif status == "unavailable":
        plural = "s" if unavailable != 1 else ""
        headline = f"Total project cost: unavailable ({unavailable} completed window{plural})"
    else:
        headline = "Total project cost: unavailable (no completed accounting)"

    lines = [headline]
    if status in {"complete", "partial"}:
        lines.extend([
            f"  Task sessions:    ${summary['task_session_cost_dollars']:.4f}",
            f"  Extra skill runs: ${summary['additional_skill_run_cost_dollars']:.4f}",
            f"  Deduped tusk runs: ${summary['deduped_tusk_skill_run_cost_dollars']:.4f}",
        ])
    lines.extend([
        "",
        "Coverage:",
        f"  task_sessions: {summary['task_sessions']['costed']} costed / "
        f"{summary['task_sessions']['total']} total "
        f"({summary['task_sessions']['unavailable']} unavailable, "
        f"{summary['task_sessions']['excluded']} excluded)",
        f"  skill_runs:    {summary['skill_runs']['included_costed']} included costed / "
        f"{summary['skill_runs']['total']} total "
        f"({summary['skill_runs']['deduped_tusk_shadows']} deduped tusk shadows, "
        f"{summary['skill_runs']['unavailable']} unavailable, "
        f"{summary['skill_runs']['excluded']} excluded)",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False,
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
