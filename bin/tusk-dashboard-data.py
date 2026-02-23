#!/usr/bin/env python3
"""Data-access layer for tusk-dashboard.py.

Provides get_connection and all fetch_* functions as a library module.
Follows the tusk-pricing-lib.py pattern: no __main__ entry point.

Imported by tusk-dashboard.py via importlib (hyphenated filename requires it).
"""

import logging
import sqlite3

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_task_metrics(conn: sqlite3.Connection) -> list[dict]:
    """Fetch per-task token and cost metrics from task_metrics view.

    Includes domain, duration, and lines changed alongside token/cost data.
    """
    log.debug("Querying task_metrics view")
    rows = conn.execute(
        """SELECT tm.id, tm.summary, tm.status,
                  tm.session_count,
                  COALESCE(tm.total_tokens_in, 0) as total_tokens_in,
                  COALESCE(tm.total_tokens_out, 0) as total_tokens_out,
                  COALESCE(tm.total_cost, 0) as total_cost,
                  tm.complexity,
                  tm.priority_score,
                  tm.github_pr,
                  tm.domain,
                  tm.task_type,
                  COALESCE(tm.total_duration_seconds, 0) as total_duration_seconds,
                  COALESCE(tm.total_lines_added, 0) as total_lines_added,
                  COALESCE(tm.total_lines_removed, 0) as total_lines_removed,
                  tm.updated_at,
                  (SELECT GROUP_CONCAT(model)
                   FROM (SELECT model, MAX(started_at) as last_used
                         FROM task_sessions s2
                         WHERE s2.task_id = tm.id AND s2.model IS NOT NULL
                         GROUP BY model
                         ORDER BY last_used DESC)) as models
           FROM task_metrics tm
           ORDER BY tm.total_cost DESC, tm.id ASC"""
    ).fetchall()
    result = [dict(r) for r in rows]
    log.debug("Fetched %d task metrics rows", len(result))
    return result


def fetch_kpi_data(conn: sqlite3.Connection) -> dict:
    """Fetch aggregated totals for KPI summary cards."""
    log.debug("Querying KPI data")
    row = conn.execute(
        """SELECT
               COALESCE(SUM(s.cost_dollars), 0) as total_cost,
               COALESCE(SUM(s.tokens_in), 0) as total_tokens_in,
               COALESCE(SUM(s.tokens_out), 0) as total_tokens_out
           FROM task_sessions s"""
    ).fetchone()

    tasks_completed = conn.execute(
        "SELECT COUNT(*) as count FROM tasks WHERE status = 'Done'"
    ).fetchone()["count"]

    tasks_total = conn.execute(
        "SELECT COUNT(*) as count FROM tasks"
    ).fetchone()["count"]

    result = {
        "total_cost": row["total_cost"],
        "total_tokens_in": row["total_tokens_in"],
        "total_tokens_out": row["total_tokens_out"],
        "total_tokens": row["total_tokens_in"] + row["total_tokens_out"],
        "tasks_completed": tasks_completed,
        "tasks_total": tasks_total,
        "avg_cost_per_task": row["total_cost"] / tasks_completed if tasks_completed > 0 else 0,
    }
    log.debug("KPI data: %s", result)
    return result


def fetch_cost_by_domain(conn: sqlite3.Connection) -> list[dict]:
    """Fetch cost grouped by domain."""
    log.debug("Querying cost by domain")
    rows = conn.execute(
        """SELECT t.domain,
                  COALESCE(SUM(s.cost_dollars), 0) as domain_cost,
                  COUNT(DISTINCT t.id) as task_count
           FROM tasks t
           LEFT JOIN task_sessions s ON s.task_id = t.id
           WHERE t.domain IS NOT NULL
           GROUP BY t.domain
           ORDER BY domain_cost DESC"""
    ).fetchall()
    result = [dict(r) for r in rows]
    log.debug("Fetched cost for %d domains", len(result))
    return result


def fetch_all_criteria(conn: sqlite3.Connection) -> dict[int, list[dict]]:
    """Fetch all acceptance criteria, grouped by task_id."""
    log.debug("Querying acceptance_criteria table")
    rows = conn.execute(
        """SELECT id, task_id, criterion, is_completed, source, cost_dollars, tokens_in, tokens_out, completed_at, criterion_type, commit_hash, committed_at
           FROM acceptance_criteria
           ORDER BY task_id, id"""
    ).fetchall()
    result: dict[int, list[dict]] = {}
    for r in rows:
        d = dict(r)
        tid = d["task_id"]
        result.setdefault(tid, []).append(d)
    log.debug("Fetched criteria for %d tasks", len(result))
    return result


def fetch_task_dependencies(conn: sqlite3.Connection) -> dict[int, dict]:
    """Fetch task dependencies, indexed by task_id with blocked_by and blocks lists."""
    log.debug("Querying task_dependencies table")
    rows = conn.execute(
        """SELECT task_id, depends_on_id, relationship_type
           FROM task_dependencies"""
    ).fetchall()
    result: dict[int, dict] = {}
    for r in rows:
        tid = r["task_id"]
        dep_id = r["depends_on_id"]
        rel = r["relationship_type"]
        result.setdefault(tid, {"blocked_by": [], "blocks": []})
        result[tid]["blocked_by"].append({"id": dep_id, "type": rel})
        result.setdefault(dep_id, {"blocked_by": [], "blocks": []})
        result[dep_id]["blocks"].append({"id": tid, "type": rel})
    log.debug("Fetched dependencies for %d tasks", len(result))
    return result


# ---------------------------------------------------------------------------
# DAG-specific data fetching
# ---------------------------------------------------------------------------

def fetch_dag_tasks(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all tasks with metrics and criteria counts for DAG rendering."""
    log.debug("Querying task_metrics view with criteria counts for DAG")
    rows = conn.execute(
        """SELECT tm.id, tm.summary, tm.status, tm.priority, tm.domain,
                  tm.task_type, tm.complexity, tm.priority_score,
                  COALESCE(tm.session_count, 0) as session_count,
                  COALESCE(tm.total_tokens_in, 0) as total_tokens_in,
                  COALESCE(tm.total_tokens_out, 0) as total_tokens_out,
                  COALESCE(tm.total_cost, 0) as total_cost,
                  COALESCE(tm.total_duration_seconds, 0) as total_duration_seconds,
                  COALESCE(ac.criteria_total, 0) as criteria_total,
                  COALESCE(ac.criteria_done, 0) as criteria_done
           FROM task_metrics tm
           LEFT JOIN (
               SELECT task_id,
                      COUNT(*) as criteria_total,
                      SUM(is_completed) as criteria_done
               FROM acceptance_criteria
               GROUP BY task_id
           ) ac ON ac.task_id = tm.id
           ORDER BY tm.id ASC"""
    ).fetchall()
    result = [dict(r) for r in rows]
    log.debug("Fetched %d DAG tasks", len(result))
    return result


def fetch_edges(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all dependency edges for DAG."""
    log.debug("Querying task_dependencies for DAG")
    rows = conn.execute(
        """SELECT task_id, depends_on_id, relationship_type
           FROM task_dependencies"""
    ).fetchall()
    result = [dict(r) for r in rows]
    log.debug("Fetched %d edges", len(result))
    return result


def fetch_blockers(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all external blockers for DAG."""
    log.debug("Querying external_blockers for DAG")
    rows = conn.execute(
        """SELECT id, task_id, description, blocker_type, is_resolved
           FROM external_blockers"""
    ).fetchall()
    result = [dict(r) for r in rows]
    log.debug("Fetched %d blockers", len(result))
    return result


def fetch_skill_runs(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all skill runs sorted by most recent first.

    Returns an empty list if the skill_runs table does not exist (pre-migration DB).
    """
    log.debug("Querying skill_runs table")
    try:
        rows = conn.execute(
            """SELECT id, skill_name, started_at, ended_at, cost_dollars, tokens_in, tokens_out, model, metadata
               FROM skill_runs
               ORDER BY started_at DESC"""
        ).fetchall()
    except sqlite3.OperationalError:
        log.warning("skill_runs table not found — run 'tusk migrate' to create it")
        return []
    result = [dict(r) for r in rows]
    log.debug("Fetched %d skill runs", len(result))
    return result


def fetch_tool_call_stats_per_task(conn: sqlite3.Connection) -> list[dict]:
    """Fetch per-task tool call aggregates (all tools per task).

    Returns an empty list if the tool_call_stats table does not exist.
    """
    log.debug("Querying tool_call_stats for per-task aggregates")
    try:
        rows = conn.execute(
            """SELECT tcs.task_id,
                      COALESCE(t.summary, '(Task ' || tcs.task_id || ')') as task_summary,
                      tcs.tool_name,
                      SUM(tcs.call_count) as call_count,
                      SUM(tcs.total_cost) as total_cost,
                      MAX(tcs.max_cost) as max_cost,
                      SUM(tcs.tokens_in) as tokens_in
               FROM tool_call_stats tcs
               LEFT JOIN tasks t ON tcs.task_id = t.id
               WHERE tcs.task_id IS NOT NULL
                 AND tcs.session_id IS NOT NULL
               GROUP BY tcs.task_id, tcs.tool_name
               ORDER BY tcs.task_id, total_cost DESC"""
        ).fetchall()
    except sqlite3.OperationalError:
        log.warning("tool_call_stats table not found — run 'tusk migrate' to create it")
        return []
    result = [dict(r) for r in rows]
    log.debug("Fetched %d per-task tool call stat rows", len(result))
    return result


def fetch_tool_call_stats_per_skill_run(conn: sqlite3.Connection) -> list[dict]:
    """Fetch per-skill-run tool call rows.

    Returns an empty list if the tool_call_stats table or skill_run_id column
    does not exist (pre-migration DB).
    """
    log.debug("Querying tool_call_stats for per-skill-run aggregates")
    try:
        rows = conn.execute(
            """SELECT skill_run_id, tool_name, call_count, total_cost, max_cost, tokens_in
               FROM tool_call_stats
               WHERE skill_run_id IS NOT NULL
               ORDER BY skill_run_id, total_cost DESC"""
        ).fetchall()
    except sqlite3.OperationalError:
        log.warning("tool_call_stats skill_run_id column not found — run 'tusk migrate' to update schema")
        return []
    result = [dict(r) for r in rows]
    log.debug("Fetched %d per-skill-run tool call stat rows", len(result))
    return result


def fetch_tool_call_stats_per_criterion(conn: sqlite3.Connection) -> list[dict]:
    """Fetch per-criterion tool call rows.

    Returns an empty list if the criterion_id column does not exist (pre-migration DB).
    """
    log.debug("Querying tool_call_stats for per-criterion aggregates")
    try:
        rows = conn.execute(
            """SELECT criterion_id, tool_name, call_count, total_cost, max_cost, tokens_in
               FROM tool_call_stats
               WHERE criterion_id IS NOT NULL
               ORDER BY criterion_id, total_cost DESC"""
        ).fetchall()
    except sqlite3.OperationalError:
        log.warning("tool_call_stats criterion_id column not found — run 'tusk migrate' to update schema")
        return []
    result = [dict(r) for r in rows]
    log.debug("Fetched %d per-criterion tool call stat rows", len(result))
    return result


def fetch_tool_call_stats_global(conn: sqlite3.Connection) -> list[dict]:
    """Fetch project-wide tool call aggregates across all task sessions.

    Aggregates session_id-attributed rows only to avoid double-counting with
    criterion rows (which share the same transcript window as their parent session).
    Returns an empty list if the tool_call_stats table does not exist.
    """
    log.debug("Querying tool_call_stats for project-wide aggregates")
    try:
        rows = conn.execute(
            """SELECT tool_name,
                      SUM(call_count) as total_calls,
                      SUM(total_cost) as total_cost,
                      SUM(tokens_in) as tokens_in
               FROM tool_call_stats
               WHERE session_id IS NOT NULL
               GROUP BY tool_name
               ORDER BY total_cost DESC"""
        ).fetchall()
    except sqlite3.OperationalError:
        log.warning("tool_call_stats table not found — run 'tusk migrate' to create it")
        return []
    result = [dict(r) for r in rows]
    log.debug("Fetched %d global tool call stat rows", len(result))
    return result


def fetch_cost_trend(conn: sqlite3.Connection) -> list[dict]:
    """Fetch weekly cost aggregations from task_sessions."""
    log.debug("Querying cost trend data")
    rows = conn.execute(
        """SELECT date(started_at, 'weekday 0', '-6 days') as week_start,
                  SUM(COALESCE(cost_dollars, 0)) as weekly_cost
           FROM task_sessions
           WHERE cost_dollars > 0
           GROUP BY week_start
           ORDER BY week_start"""
    ).fetchall()
    result = [dict(r) for r in rows]
    log.debug("Fetched %d weekly cost buckets", len(result))
    return result


def fetch_cost_trend_daily(conn: sqlite3.Connection) -> list[dict]:
    """Fetch daily cost aggregations from task_sessions."""
    log.debug("Querying daily cost trend data")
    rows = conn.execute(
        """SELECT date(started_at) as day,
                  SUM(COALESCE(cost_dollars, 0)) as daily_cost
           FROM task_sessions
           WHERE cost_dollars > 0
           GROUP BY day
           ORDER BY day"""
    ).fetchall()
    result = [dict(r) for r in rows]
    log.debug("Fetched %d daily cost buckets", len(result))
    return result


def fetch_cost_trend_monthly(conn: sqlite3.Connection) -> list[dict]:
    """Fetch monthly cost aggregations from task_sessions."""
    log.debug("Querying monthly cost trend data")
    rows = conn.execute(
        """SELECT strftime('%Y-%m', started_at) as month,
                  SUM(COALESCE(cost_dollars, 0)) as monthly_cost
           FROM task_sessions
           WHERE cost_dollars > 0
           GROUP BY month
           ORDER BY month"""
    ).fetchall()
    result = [dict(r) for r in rows]
    log.debug("Fetched %d monthly cost buckets", len(result))
    return result


def fetch_complexity_metrics(conn: sqlite3.Connection) -> list[dict]:
    """Fetch average session count, duration, and cost grouped by complexity for completed tasks."""
    log.debug("Querying complexity metrics")
    rows = conn.execute(
        """SELECT t.complexity,
                  COUNT(*) as task_count,
                  ROUND(AVG(COALESCE(m.session_count, 0)), 1) as avg_sessions,
                  ROUND(AVG(COALESCE(m.total_duration_seconds, 0))) as avg_duration_seconds,
                  ROUND(AVG(COALESCE(m.total_cost, 0)), 2) as avg_cost
           FROM tasks t
           LEFT JOIN (
               SELECT task_id,
                      COUNT(id) as session_count,
                      SUM(duration_seconds) as total_duration_seconds,
                      SUM(cost_dollars) as total_cost
               FROM task_sessions
               GROUP BY task_id
           ) m ON m.task_id = t.id
           WHERE t.status = 'Done' AND t.complexity IS NOT NULL
           GROUP BY t.complexity
           ORDER BY CASE t.complexity
               WHEN 'XS' THEN 1
               WHEN 'S' THEN 2
               WHEN 'M' THEN 3
               WHEN 'L' THEN 4
               WHEN 'XL' THEN 5
               ELSE 6
           END"""
    ).fetchall()
    result = [dict(r) for r in rows]
    log.debug("Fetched %d complexity metric rows", len(result))
    return result
