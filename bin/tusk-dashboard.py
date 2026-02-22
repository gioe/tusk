#!/usr/bin/env python3
"""Generate a static HTML dashboard for tusk task databases.

Decomposes the dashboard into focused sub-functions with a CSS design system,
KPI summary cards, and per-task metrics.

Called by the tusk wrapper:
    tusk dashboard

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
"""

import html
import importlib.util
import json
import logging
import os
import re
import sqlite3
import sys
import webbrowser
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _load_dashboard_css_module():
    """Import tusk-dashboard-css.py (hyphenated filename requires importlib)."""
    cached = sys.modules.get("tusk_dashboard_css")
    if cached is not None:
        return cached
    lib_path = Path(__file__).resolve().parent / "tusk-dashboard-css.py"
    spec = importlib.util.spec_from_file_location("tusk_dashboard_css", lib_path)
    if spec is None:
        raise FileNotFoundError(f"CSS companion module not found: {lib_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tusk_dashboard_css"] = mod
    spec.loader.exec_module(mod)
    return mod

log = logging.getLogger(__name__)

# Expected session ranges per complexity tier (from CLAUDE.md)
EXPECTED_SESSIONS = {
    'XS': (0.5, 1),
    'S': (1, 1.5),
    'M': (1, 2),
    'L': (3, 5),
    'XL': (5, 10),
}


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
# DAG-specific data fetching (ported from tusk-dag.py)
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
                      MAX(tcs.max_cost) as max_cost
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
            """SELECT skill_run_id, tool_name, call_count, total_cost, max_cost
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
            """SELECT criterion_id, tool_name, call_count, total_cost, max_cost
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
                      SUM(total_cost) as total_cost
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


def filter_dag_nodes(tasks: list[dict], edges: list[dict], blockers: list[dict],
                     show_all: bool) -> tuple[list[dict], list[dict], list[dict]]:
    """Filter tasks, edges, and blockers for DAG visibility.

    Default: all To Do + In Progress tasks, plus Done tasks with >= 1 edge.
    show_all: additionally include isolated Done tasks.
    Prunes connected components where every task is Done (unless show_all).
    """
    edge_task_ids = set()
    for e in edges:
        edge_task_ids.add(e["task_id"])
        edge_task_ids.add(e["depends_on_id"])

    visible_tasks = []
    for t in tasks:
        if t["status"] in ("To Do", "In Progress"):
            visible_tasks.append(t)
        elif t["status"] == "Done":
            if show_all or t["id"] in edge_task_ids:
                visible_tasks.append(t)

    visible_ids = {t["id"] for t in visible_tasks}

    if not show_all:
        adj: dict[int, set] = defaultdict(set)
        for e in edges:
            a, b = e["task_id"], e["depends_on_id"]
            if a in visible_ids and b in visible_ids:
                adj[a].add(b)
                adj[b].add(a)

        status_map = {t["id"]: t["status"] for t in visible_tasks}
        visited: set[int] = set()
        remove_ids: set[int] = set()
        for tid in visible_ids:
            if tid in visited:
                continue
            queue = deque([tid])
            component: list[int] = []
            while queue:
                node = queue.popleft()
                if node in visited:
                    continue
                visited.add(node)
                component.append(node)
                for neighbor in adj[node]:
                    if neighbor not in visited:
                        queue.append(neighbor)
            if all(status_map[n] == "Done" for n in component):
                remove_ids.update(component)

        if remove_ids:
            visible_tasks = [t for t in visible_tasks if t["id"] not in remove_ids]
            visible_ids -= remove_ids

    visible_edges = [
        e for e in edges
        if e["task_id"] in visible_ids and e["depends_on_id"] in visible_ids
    ]
    visible_blockers = [b for b in blockers if b["task_id"] in visible_ids]

    log.debug("DAG visible: %d tasks, %d edges, %d blockers",
              len(visible_tasks), len(visible_edges), len(visible_blockers))
    return visible_tasks, visible_edges, visible_blockers


def build_mermaid(tasks: list[dict], edges: list[dict], blockers: list[dict]) -> str:
    """Build Mermaid graph definition from tasks, edges, and blockers."""
    lines = ["graph LR"]

    lines.append('    classDef todo fill:#3b82f6,stroke:#2563eb,color:#fff')
    lines.append('    classDef inprogress fill:#f59e0b,stroke:#d97706,color:#fff')
    lines.append('    classDef done fill:#22c55e,stroke:#16a34a,color:#fff')
    lines.append('    classDef blocker fill:#ef4444,stroke:#dc2626,color:#fff')
    lines.append('    classDef blockerResolved fill:#9ca3af,stroke:#6b7280,color:#fff')

    for t in tasks:
        node_id = "T" + str(t["id"])
        summary = t["summary"] or ""
        if len(summary) > 40:
            summary = summary[:37] + "..."
        summary = summary.replace('"', "'")
        label = "#" + str(t["id"]) + ": " + summary
        complexity = t["complexity"] or "S"

        if complexity in ("XS", "S"):
            node_def = node_id + '["' + label + '"]'
        elif complexity == "M":
            node_def = node_id + '("' + label + '")'
        else:
            node_def = node_id + '{{"' + label + '"}}'

        lines.append("    " + node_def)

        status = t["status"]
        if status == "To Do":
            lines.append("    class " + node_id + " todo")
        elif status == "In Progress":
            lines.append("    class " + node_id + " inprogress")
        elif status == "Done":
            lines.append("    class " + node_id + " done")

    for b in blockers:
        node_id = "B" + str(b["id"])
        desc = b["description"] or ""
        if len(desc) > 35:
            desc = desc[:32] + "..."
        desc = desc.replace('"', "'")
        btype = b["blocker_type"] or "external"
        label = btype + ": " + desc
        node_def = node_id + '>"' + label + '"]'
        lines.append("    " + node_def)

        if b["is_resolved"]:
            lines.append("    class " + node_id + " blockerResolved")
        else:
            lines.append("    class " + node_id + " blocker")

    for e in edges:
        src = "T" + str(e["depends_on_id"])
        dst = "T" + str(e["task_id"])
        if e["relationship_type"] == "contingent":
            lines.append("    " + src + " -.-> " + dst)
        else:
            lines.append("    " + src + " --> " + dst)

    for b in blockers:
        src = "B" + str(b["id"])
        dst = "T" + str(b["task_id"])
        lines.append("    " + src + " -.-x " + dst)

    for t in tasks:
        node_id = "T" + str(t["id"])
        lines.append('    click ' + node_id + ' dagShowSidebar')

    for b in blockers:
        node_id = "B" + str(b["id"])
        lines.append('    click ' + node_id + ' dagShowBlockerSidebar')

    return "\n".join(lines)


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


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def esc(text) -> str:
    """HTML-escape a value, handling None."""
    if text is None:
        return ""
    return html.escape(str(text))


def format_number(n) -> str:
    """Format a number with commas."""
    if n is None:
        return "0"
    return f"{int(n):,}"


def format_cost(c) -> str:
    """Format a dollar amount."""
    if c is None or c == 0:
        return "$0.00"
    return f"${c:,.2f}"


def format_duration(seconds) -> str:
    """Format seconds as a human-readable duration."""
    if seconds is None or seconds == 0:
        return "0m"
    hours = int(seconds) // 3600
    minutes = (int(seconds) % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_date(dt_str) -> str:
    """Format an ISO datetime string as YYYY-MM-DD HH:MM:SS in local timezone."""
    if dt_str is None:
        return '<span class="text-muted-dash">&mdash;</span>'
    dt = _parse_dt(dt_str)
    if dt is None:
        return esc(dt_str)
    local_dt = dt.astimezone()
    if local_dt.microsecond:
        return local_dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{local_dt.microsecond // 1000:03d}"
    return local_dt.strftime("%Y-%m-%d %H:%M:%S")


def format_tokens_compact(n) -> str:
    """Format token count compactly (e.g., 1.6M, 234K, 56)."""
    if n is None or n == 0:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))



def format_relative_time(dt_str) -> str:
    """Format a datetime string as relative time (e.g., 2h ago, 3d ago)."""
    if dt_str is None:
        return ""
    dt = _parse_dt(dt_str)
    if dt is None:
        return ""
    seconds = int((datetime.now(timezone.utc) - dt).total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 604800:
        return f"{seconds // 86400}d ago"
    if seconds < 2592000:
        return f"{seconds // 604800}w ago"
    if seconds < 31536000:
        return f"{seconds // 2592000}mo ago"
    return f"{seconds // 31536000}y ago"


# ---------------------------------------------------------------------------
# HTML generation sub-functions
# ---------------------------------------------------------------------------

def generate_css() -> str:
    """Generate the full CSS with design system tokens."""
    return _load_dashboard_css_module().CSS


def generate_header(now: str) -> str:
    """Generate the page header bar with theme toggle and tab navigation."""
    return f"""\
<div class="header">
  <h1>Tusk &mdash; Task Metrics</h1>
  <div style="display:flex;align-items:center;gap:var(--sp-3);">
    <span class="timestamp">Generated {esc(now)}</span>
    <button class="theme-toggle" id="themeToggle" title="Toggle dark mode" aria-label="Toggle dark mode">
      <span class="icon-sun">\u2600\uFE0F</span>
      <span class="icon-moon">\U0001F319</span>
    </button>
  </div>
</div>
<div class="tab-bar" id="tabBar">
  <button class="tab-btn active" data-tab="dashboard">Tasks</button>
  <button class="tab-btn" data-tab="dag">DAG</button>
  <button class="tab-btn" data-tab="skills">Skill</button>
</div>"""


def generate_footer(now: str, version: str) -> str:
    """Generate the page footer with timestamp and version."""
    return f"""\
<div class="footer">
  <span>Generated {esc(now)}</span>
  <span>tusk v{esc(version)}</span>
</div>"""


def generate_kpi_cards(kpi_data: dict) -> str:
    """Generate 6 KPI summary cards."""
    total_cost = format_cost(kpi_data["total_cost"])
    tasks_completed = kpi_data["tasks_completed"]
    tasks_total = kpi_data["tasks_total"]
    avg_cost = format_cost(kpi_data["avg_cost_per_task"])
    total_tokens = format_tokens_compact(kpi_data["total_tokens"])
    tokens_in = format_tokens_compact(kpi_data["total_tokens_in"])
    tokens_out = format_tokens_compact(kpi_data["total_tokens_out"])
    return f"""\
<div class="kpi-grid">
  <div class="kpi-card">
    <div class="kpi-label">Total Cost</div>
    <div class="kpi-value">{total_cost}</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Tasks Completed</div>
    <div class="kpi-value">{tasks_completed}</div>
    <div class="kpi-sub">of {tasks_total} total</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Avg Cost / Task</div>
    <div class="kpi-value">{avg_cost}</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Total Tokens</div>
    <div class="kpi-value">{total_tokens}</div>
    <div class="kpi-sub">{tokens_in} in / {tokens_out} out</div>
  </div>
</div>"""


def _parse_dt(dt_str: str) -> datetime | None:
    """Parse a datetime string (assumed UTC) and return a UTC-aware datetime."""
    if not dt_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(dt_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def generate_skill_runs_section(skill_runs: list[dict], tool_stats_by_run: dict = None) -> str:
    """Generate the Skill Run Costs section with summary cards, charts, and enriched table."""
    if tool_stats_by_run is None:
        tool_stats_by_run = {}
    if not skill_runs:
        return """\
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header">Skill Run Costs</div>
  <p class="empty" style="padding: var(--sp-4);">No skill runs recorded yet.</p>
</div>"""

    # --- Aggregate cost per skill ---
    skill_totals: dict[str, float] = defaultdict(float)
    for r in skill_runs:
        skill_totals[r['skill_name']] += r.get('cost_dollars') or 0

    # --- Summary stats ---
    total_runs = len(skill_runs)
    total_cost = sum(r.get('cost_dollars') or 0 for r in skill_runs)
    avg_cost = total_cost / total_runs if total_runs else 0
    most_expensive_skill = max(skill_totals, key=lambda k: skill_totals[k]) if skill_totals else "\u2014"

    # --- Top-3 most expensive individual runs (only badge when > 3 total) ---
    top3_ids = (
        {r['id'] for r in sorted(skill_runs, key=lambda x: x.get('cost_dollars') or 0, reverse=True)[:3]}
        if total_runs > 3 else set()
    )

    # --- Cost intensity thresholds for color-coding ---
    all_costs = [r.get('cost_dollars') or 0 for r in skill_runs]
    max_cost = max(all_costs) if all_costs else 0

    def cost_cell_style(cost: float) -> str:
        if max_cost <= 0 or cost <= 0:
            return "text-align:right;font-variant-numeric:tabular-nums;"
        ratio = cost / max_cost
        if ratio >= 0.8:
            bg = "background-color:#fde68a;"
        elif ratio >= 0.5:
            bg = "background-color:#fef3c7;"
        elif ratio >= 0.2:
            bg = "background-color:#ecfdf5;"
        else:
            bg = ""
        return f"text-align:right;font-variant-numeric:tabular-nums;{bg}"

    # --- Build table rows (most recent first) ---
    table_rows = ""
    for r in skill_runs:
        cost = r.get('cost_dollars') or 0
        cost_str = f"${cost:.4f}"
        tokens_in_str = format_tokens_compact(r.get('tokens_in') or 0)
        tokens_out_str = format_tokens_compact(r.get('tokens_out') or 0)
        model_str = esc(r.get('model') or '')
        date_str = format_date(r.get('started_at'))
        skill_str = esc(r.get('skill_name') or '')

        start_dt = _parse_dt(r.get('started_at') or '')
        end_dt = _parse_dt(r.get('ended_at') or '')
        if start_dt and end_dt:
            dur_secs = (end_dt - start_dt).total_seconds()
            dur_str = format_duration(dur_secs)
        else:
            dur_str = '<span class="text-muted-dash">&mdash;</span>'

        is_top3 = r['id'] in top3_ids
        badge = (
            ' <span style="background:#f59e0b;color:#fff;font-size:0.65rem;'
            'padding:1px 5px;border-radius:9999px;font-weight:700;vertical-align:middle;">TOP</span>'
            if is_top3 else ''
        )
        row_style = ' style="font-weight:600;"' if is_top3 else ''

        run_tool_stats = tool_stats_by_run.get(r['id'], [])
        tool_panel_html = _generate_tool_stats_panel(run_tool_stats)

        table_rows += (
            f"<tr{row_style}>"
            f"<td>{r['id']}</td>"
            f"<td>{skill_str}{badge}</td>"
            f"<td class=\"text-muted\">{date_str}</td>"
            f"<td style=\"{cost_cell_style(cost)}\">{cost_str}</td>"
            f"<td style=\"text-align:right\">{tokens_in_str}</td>"
            f"<td style=\"text-align:right\">{tokens_out_str}</td>"
            f"<td class=\"text-muted\">{dur_str}</td>"
            f"<td class=\"text-muted\">{model_str}</td>"
            f"</tr>\n"
        )
        if tool_panel_html:
            table_rows += (
                f'<tr><td colspan="8" style="padding:0;">'
                f'{tool_panel_html}'
                f'</td></tr>\n'
            )

    # --- Horizontal bar chart: total cost per skill (sorted descending) ---
    bar_labels = []
    bar_values = []
    for sk, total in sorted(skill_totals.items(), key=lambda x: x[1], reverse=True):
        bar_labels.append(sk)
        bar_values.append(round(total, 4))

    # --- Line chart: per-run cost over last 30 days, top-5 skills by total cost ---
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=30)
    top_skills = [sk for sk, _ in sorted(skill_totals.items(), key=lambda x: x[1], reverse=True)[:5]]

    # Accumulate daily cost per top skill
    skill_date_costs: dict[str, dict[str, float]] = {sk: {} for sk in top_skills}
    all_date_keys: set[str] = set()
    for r in skill_runs:
        sk = r.get('skill_name')
        if sk not in top_skills:
            continue
        start_dt_r = _parse_dt(r.get('started_at') or '')
        if start_dt_r is None or start_dt_r < cutoff_dt:
            continue
        local_start = start_dt_r.astimezone()
        day_key = local_start.strftime("%Y-%m-%d")
        all_date_keys.add(day_key)
        label = local_start.strftime("%b %d")
        skill_date_costs[sk][label] = skill_date_costs[sk].get(label, 0) + (r.get('cost_dollars') or 0)

    # Sort date labels chronologically
    sorted_day_keys = sorted(all_date_keys)
    line_labels = []
    for dk in sorted_day_keys:
        try:
            line_labels.append(datetime.strptime(dk, "%Y-%m-%d").strftime("%b %d"))
        except ValueError:
            line_labels.append(dk)

    palette = ['#3b82f6', '#f59e0b', '#22c55e', '#ef4444', '#8b5cf6']
    line_datasets = []
    for i, sk in enumerate(top_skills):
        data_points = [round(skill_date_costs[sk].get(lbl, 0), 4) for lbl in line_labels]
        line_datasets.append({
            "label": sk,
            "data": data_points,
            "borderColor": palette[i % len(palette)],
            "backgroundColor": palette[i % len(palette)] + "33",
            "tension": 0.3,
            "fill": False,
        })

    chart_data_json = json.dumps({
        "bar": {"labels": bar_labels, "values": bar_values},
        "line": {"labels": line_labels, "datasets": line_datasets},
    }).replace("</", "<\\/")

    # --- Stat cards HTML ---
    stat_cards_html = f"""\
<div class="kpi-grid" style="padding:var(--sp-4);margin-bottom:0;">
  <div class="kpi-card">
    <div class="kpi-label">Total Runs</div>
    <div class="kpi-value">{total_runs}</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Total Cost</div>
    <div class="kpi-value">${total_cost:.4f}</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Avg Cost / Run</div>
    <div class="kpi-value">${avg_cost:.4f}</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Priciest Skill</div>
    <div class="kpi-value" style="font-size:var(--text-base);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="{esc(most_expensive_skill)}">{esc(most_expensive_skill)}</div>
  </div>
</div>"""

    bar_chart_height = max(80, len(skill_totals) * 25)
    line_chart_section = (
        f'<div class="section-header section-header--bordered">'
        f'Cost Trend \u2014 Last 30 Days (Top {len(top_skills)} Skills)</div>'
        f'<div class="dash-chart-wrap"><canvas id="skillLineChart" height="100"></canvas></div>'
        if line_labels else
        '<div class="section-header section-header--bordered">Cost Trend \u2014 Last 30 Days</div>'
        '<p class="text-muted" style="padding:var(--sp-4);">No runs in the last 30 days.</p>'
    )
    charts_html = f"""\
<div class="section-header section-header--bordered">Cost by Skill (Total)</div>
<div class="dash-chart-wrap">
  <canvas id="skillBarChart" height="{bar_chart_height}"></canvas>
</div>
{line_chart_section}"""

    charts_script = f"""\
<script>
(function() {{
  var chartData = {chart_data_json};
  var palette = ['#3b82f6','#f59e0b','#22c55e','#ef4444','#8b5cf6'];

  // Horizontal bar chart: total cost per skill
  var barCanvas = document.getElementById('skillBarChart');
  if (barCanvas && chartData.bar.labels.length > 0) {{
    new Chart(barCanvas, {{
      type: 'bar',
      data: {{
        labels: chartData.bar.labels,
        datasets: [{{
          label: 'Total Cost',
          data: chartData.bar.values,
          backgroundColor: chartData.bar.labels.map(function(_, i) {{ return palette[i % palette.length] + '99'; }}),
          borderColor: chartData.bar.labels.map(function(_, i) {{ return palette[i % palette.length]; }}),
          borderWidth: 1
        }}]
      }},
      options: {{
        indexAxis: 'y',
        responsive: true,
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{ callbacks: {{ label: function(c) {{ return '$' + c.parsed.x.toFixed(4); }} }} }}
        }},
        scales: {{
          x: {{ beginAtZero: true, ticks: {{ callback: function(v) {{ return '$' + v.toFixed(3); }} }} }}
        }}
      }}
    }});
  }}

  // Line chart: per-run cost trend over time per skill
  var lineCanvas = document.getElementById('skillLineChart');
  if (lineCanvas && chartData.line.labels.length > 0) {{
    new Chart(lineCanvas, {{
      type: 'line',
      data: {{
        labels: chartData.line.labels,
        datasets: chartData.line.datasets
      }},
      options: {{
        responsive: true,
        plugins: {{
          legend: {{ position: 'top' }},
          tooltip: {{ callbacks: {{ label: function(c) {{ return c.dataset.label + ': $' + c.parsed.y.toFixed(4); }} }} }}
        }},
        scales: {{
          y: {{ beginAtZero: true, ticks: {{ callback: function(v) {{ return '$' + v.toFixed(3); }} }} }}
        }}
      }}
    }});
  }}
}})();
</script>"""

    return f"""\
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header">Skill Run Costs</div>
  {stat_cards_html}
  {charts_html}
  <div class="section-header section-header--bordered">All Runs</div>
  <div class="dash-table-scroll">
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Skill</th>
          <th>Date</th>
          <th style="text-align:right">Cost</th>
          <th style="text-align:right">Tokens In</th>
          <th style="text-align:right">Tokens Out</th>
          <th>Duration</th>
          <th>Model</th>
        </tr>
      </thead>
      <tbody>
        {table_rows}
      </tbody>
    </table>
  </div>
</div>{charts_script}"""


def _generate_tool_stats_panel(tool_stats: list[dict]) -> str:
    """Generate a collapsible tool cost breakdown panel for a single task row.

    Rendered server-side; inserted inside the expanded criteria row.
    Returns an empty string when tool_stats is empty.
    """
    if not tool_stats:
        return ""
    task_total = sum(r["total_cost"] or 0 for r in tool_stats)
    tool_rows = ""
    for r in tool_stats:
        tool_cost = r["total_cost"] or 0
        tool_pct = (tool_cost / task_total * 100) if task_total > 0 else 0
        tool_rows += (
            f'<tr class="tc-row">'
            f'<td class="tc-tool">{esc(r["tool_name"])}</td>'
            f'<td class="tc-calls" style="text-align:right;font-variant-numeric:tabular-nums;">{int(r["call_count"] or 0):,}</td>'
            f'<td class="tc-cost" style="text-align:right;font-variant-numeric:tabular-nums;">${tool_cost:.4f}</td>'
            f'<td class="tc-pct" style="min-width:100px;">'
            f'<div style="display:flex;align-items:center;gap:6px;">'
            f'<div style="flex:1;background:var(--border);border-radius:3px;height:8px;overflow:hidden;">'
            f'<div style="width:{tool_pct:.1f}%;background:var(--accent,#3b82f6);height:100%;border-radius:3px;"></div>'
            f'</div>'
            f'<span style="font-size:0.75rem;color:var(--text-muted,#6b7280);min-width:36px;">{tool_pct:.1f}%</span>'
            f'</div>'
            f'</td>'
            f'</tr>\n'
        )
    return (
        f'<details class="tc-task-panel tc-task-panel--bordered">'
        f'<summary style="padding:var(--sp-2) var(--sp-4);cursor:pointer;list-style:none;'
        f'display:flex;justify-content:space-between;align-items:center;'
        f'font-size:0.85rem;color:var(--text-muted,#6b7280);">'
        f'<span>Tool Cost Breakdown (attributed)</span>'
        f'<span style="font-variant-numeric:tabular-nums;" title="Attributed tool cost only — may be less than total session cost if some sessions lack transcripts">${task_total:.4f}</span>'
        f'</summary>'
        f'<div style="overflow-x:auto;padding:0 var(--sp-4) var(--sp-3);">'
        f'<table class="tc-table" style="margin-top:0;">'
        f'<thead><tr>'
        f'<th>Tool</th>'
        f'<th style="text-align:right">Calls</th>'
        f'<th style="text-align:right">Cost</th>'
        f'<th>Share of attributed cost</th>'
        f'</tr></thead>'
        f'<tbody>{tool_rows}</tbody>'
        f'</table>'
        f'</div>'
        f'</details>'
    )


def generate_global_tool_costs_section(tool_stats: list[dict]) -> str:
    """Generate a project-wide tool cost aggregate table for the Skills tab.

    Shows tool_name, total_calls, total_cost, and share of total across all
    task sessions. Returns a placeholder panel with onboarding instructions
    when no data is available.
    """
    if not tool_stats:
        return """\
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header">Project-Wide Tool Costs</div>
  <div style="padding:var(--sp-4);color:var(--text-muted,#6b7280);font-size:0.875rem;">
    No tool call stats yet. Run <code>tusk session-close &lt;session_id&gt;</code> to populate this panel.
  </div>
</div>"""

    grand_total = sum(r["total_cost"] or 0 for r in tool_stats)
    total_calls = sum(r["total_calls"] or 0 for r in tool_stats)

    rows_html = ""
    for r in tool_stats:
        cost = r["total_cost"] or 0
        calls = r["total_calls"] or 0
        pct = (cost / grand_total * 100) if grand_total > 0 else 0
        rows_html += (
            f'<tr>'
            f'<td style="font-weight:500;">{esc(r["tool_name"])}</td>'
            f'<td style="text-align:right;font-variant-numeric:tabular-nums;">{int(calls):,}</td>'
            f'<td style="text-align:right;font-variant-numeric:tabular-nums;">${cost:.4f}</td>'
            f'<td style="min-width:130px;">'
            f'<div style="display:flex;align-items:center;gap:6px;">'
            f'<div style="flex:1;background:var(--border);border-radius:3px;height:8px;overflow:hidden;">'
            f'<div style="width:{pct:.1f}%;background:var(--accent,#3b82f6);height:100%;border-radius:3px;"></div>'
            f'</div>'
            f'<span style="font-size:0.75rem;color:var(--text-muted,#6b7280);min-width:36px;">{pct:.1f}%</span>'
            f'</div>'
            f'</td>'
            f'</tr>\n'
        )

    return f"""\
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header">Project-Wide Tool Costs</div>
  <div class="kpi-grid" style="padding:var(--sp-4);margin-bottom:0;">
    <div class="kpi-card">
      <div class="kpi-label">Tools Used</div>
      <div class="kpi-value">{len(tool_stats)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Total Calls</div>
      <div class="kpi-value">{total_calls:,}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Total Cost (sessions)</div>
      <div class="kpi-value">${grand_total:.4f}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Costliest Tool</div>
      <div class="kpi-value" style="font-size:var(--text-base);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="{esc(tool_stats[0]['tool_name'])}">{esc(tool_stats[0]['tool_name'])}</div>
    </div>
  </div>
  <div class="section-header section-header--bordered">All Tools</div>
  <div class="dash-table-scroll">
    <table>
      <thead>
        <tr>
          <th>Tool</th>
          <th style="text-align:right">Total Calls</th>
          <th style="text-align:right">Total Cost</th>
          <th>Share of Total</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
      <tfoot>
        <tr style="font-weight:600;border-top:2px solid var(--border);">
          <td>Total</td>
          <td style="text-align:right;font-variant-numeric:tabular-nums;">{total_calls:,}</td>
          <td style="text-align:right;font-variant-numeric:tabular-nums;">${grand_total:.4f}</td>
          <td></td>
        </tr>
      </tfoot>
    </table>
  </div>
</div>"""


def _format_chart_labels(rows: list[dict], period_key: str, period_label: str) -> list[str]:
    """Format period strings into human-readable chart labels."""
    labels = []
    for row in rows:
        raw = row[period_key]
        try:
            if period_label == "Daily":
                dt = datetime.strptime(raw, "%Y-%m-%d")
                labels.append(dt.strftime("%b %d, %Y"))
            elif period_label == "Monthly":
                dt = datetime.strptime(raw + "-01", "%Y-%m-%d")
                labels.append(dt.strftime("%b %Y"))
            else:
                labels.append(f"Week of {raw}")
        except ValueError:
            labels.append(raw)
    return labels


def _build_chart_dataset(rows: list[dict], period_key: str, cost_key: str, period_label: str) -> dict:
    """Build a JSON-serializable dataset for a cost trend period."""
    labels = _format_chart_labels(rows, period_key, period_label)
    costs = [row[cost_key] for row in rows]
    cumulative = []
    running = 0.0
    for c in costs:
        running += c
        cumulative.append(round(running, 2))
    return {"labels": labels, "costs": costs, "cumulative": cumulative}


def generate_charts_section(cost_trend: list[dict], cost_trend_daily: list[dict],
                            cost_trend_monthly: list[dict], cost_by_domain: list[dict] = None) -> str:
    """Generate the charts panel with Chart.js canvases and embedded JSON data."""
    daily_data = _build_chart_dataset(cost_trend_daily, "day", "daily_cost", "Daily")
    weekly_data = _build_chart_dataset(cost_trend, "week_start", "weekly_cost", "Weekly")
    monthly_data = _build_chart_dataset(cost_trend_monthly, "month", "monthly_cost", "Monthly")

    chart_data = json.dumps({
        "daily": daily_data,
        "weekly": weekly_data,
        "monthly": monthly_data,
    }).replace("</", "<\\/")

    domain_data = json.dumps(cost_by_domain or []).replace("</", "<\\/")

    has_cost_data = any(d["costs"] for d in [daily_data, weekly_data, monthly_data])
    empty_msg = '<p class="empty">No session cost data available yet.</p>' if not has_cost_data else ''

    has_domain_data = bool(cost_by_domain and any(d["domain_cost"] > 0 for d in cost_by_domain))
    domain_empty_msg = '<p class="empty">No cost-by-domain data available yet.</p>' if not has_domain_data else ''

    return f"""\
<script>
window.__tuskCostTrend = {chart_data};
window.__tuskCostByDomain = {domain_data};
</script>
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header" style="display:flex;align-items:center;justify-content:space-between;">
    <span>Cost Trend</span>
    <div class="cost-trend-tabs" id="costTrendTabs">
      <button class="cost-tab" data-tab="daily">Daily</button>
      <button class="cost-tab active" data-tab="weekly">Weekly</button>
      <button class="cost-tab" data-tab="monthly">Monthly</button>
    </div>
  </div>
  <div style="padding: var(--sp-4);">
    {empty_msg}
    <canvas id="costTrendChart" height="260" style="max-width:850px;width:100%;{' display:none;' if not has_cost_data else ''}"></canvas>
  </div>
</div>
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header">
    <span>Cost by Domain</span>
  </div>
  <div style="padding: var(--sp-4);">
    {domain_empty_msg}
    <canvas id="costByDomainChart" height="200" style="max-width:850px;width:100%;{' display:none;' if not has_domain_data else ''}"></canvas>
  </div>
</div>"""


def generate_filter_bar() -> str:
    """Generate the filter chips, dropdowns, search input, and filter badge."""
    return """\
<div class="filter-bar">
  <div class="filter-chips" id="statusFilters">
    <button class="filter-chip active" data-filter="All">All</button>
    <button class="filter-chip" data-filter="To Do">To Do</button>
    <button class="filter-chip" data-filter="In Progress">In Progress</button>
    <button class="filter-chip" data-filter="Done">Done</button>
  </div>
  <select class="filter-select" id="domainFilter"><option value="">Domain</option></select>
  <select class="filter-select" id="complexityFilter"><option value="">Size</option></select>
  <select class="filter-select" id="typeFilter"><option value="">Type</option></select>
  <input type="text" class="search-input" id="searchInput" placeholder="Search tasks\u2026">
  <div class="filter-meta">
    <span class="filter-badge hidden" id="filterBadge">0</span>
    <button class="clear-filters hidden" id="clearFilters">Clear all</button>
  </div>
</div>"""


def generate_table_header() -> str:
    """Generate the table thead."""
    return """\
<thead>
  <tr>
    <th data-col="0" data-type="num">ID <span class="sort-arrow">\u25B2</span></th>
    <th data-col="1" data-type="str">Task <span class="sort-arrow">\u25B2</span></th>
    <th data-col="2" data-type="str">Status <span class="sort-arrow">\u25B2</span></th>
    <th data-col="3" data-type="str">Domain <span class="sort-arrow">\u25B2</span></th>
    <th data-col="4" data-type="num">Size <span class="sort-arrow">\u25B2</span></th>
    <th data-col="5" data-type="num" style="text-align:right">WSJF <span class="sort-arrow">\u25B2</span></th>
    <th data-col="6" data-type="num" style="text-align:right">Sessions <span class="sort-arrow">\u25B2</span></th>
    <th data-col="7" data-type="str">Model <span class="sort-arrow">\u25B2</span></th>
    <th data-col="8" data-type="num" style="text-align:right">Duration <span class="sort-arrow">\u25B2</span></th>
    <th data-col="9" data-type="num" style="text-align:right">Lines <span class="sort-arrow">\u25B2</span></th>
    <th data-col="10" data-type="num" style="text-align:right">Tokens In <span class="sort-arrow">\u25B2</span></th>
    <th data-col="11" data-type="num" style="text-align:right">Tokens Out <span class="sort-arrow">\u25B2</span></th>
    <th data-col="12" data-type="num" style="text-align:right">Cost <span class="sort-arrow">\u25B2</span></th>
    <th data-col="13" data-type="str" class="sort-desc">Updated <span class="sort-arrow">\u25BC</span></th>
  </tr>
</thead>"""


def build_dep_badges(tid: int, task_deps: dict, summary_map: dict) -> str:
    """Build HTML for dependency badges, or empty string if none."""
    deps = task_deps.get(tid)
    if not deps:
        return ""
    blocked_by = deps.get("blocked_by", [])
    blocks = deps.get("blocks", [])
    if not blocked_by and not blocks:
        return ""
    parts = []
    if blocked_by:
        badges = []
        for d in blocked_by:
            tooltip = esc(summary_map.get(d["id"], f"Task #{d['id']}"))
            css = f'dep-link dep-type-{esc(d["type"])}'
            badges.append(
                f'<a class="{css}" data-target="{d["id"]}" title="{tooltip}">#{d["id"]}</a>'
            )
        parts.append(
            f'<span class="dep-group"><span class="dep-label">Blocked by</span> {"".join(badges)}</span>'
        )
    if blocks:
        badges = []
        for d in blocks:
            tooltip = esc(summary_map.get(d["id"], f"Task #{d['id']}"))
            css = f'dep-link dep-type-{esc(d["type"])}'
            badges.append(
                f'<a class="{css}" data-target="{d["id"]}" title="{tooltip}">#{d["id"]}</a>'
            )
        parts.append(
            f'<span class="dep-group"><span class="dep-label">Blocks</span> {"".join(badges)}</span>'
        )
    return f'<div class="dep-badges">{"".join(parts)}</div>'


COMPLEXITY_SORT_ORDER = {'XS': 1, 'S': 2, 'M': 3, 'L': 4, 'XL': 5}


def cost_heat_class(cost: float, max_cost: float) -> str:
    """Return a CSS class for cost heatmap tinting."""
    if max_cost <= 0 or cost <= 0:
        return ""
    ratio = cost / max_cost
    if ratio < 0.10:
        return ""
    if ratio < 0.25:
        return "cost-heat-1"
    if ratio < 0.45:
        return "cost-heat-2"
    if ratio < 0.65:
        return "cost-heat-3"
    if ratio < 0.85:
        return "cost-heat-4"
    return "cost-heat-5"


def format_lines_html(added, removed) -> str:
    """Format lines changed as colored +N / -M HTML."""
    added = added or 0
    removed = removed or 0
    if added == 0 and removed == 0:
        return '<span class="text-muted-dash">&mdash;</span>'
    parts = []
    if added > 0:
        parts.append(f'<span class="lines-added">+{int(added)}</span>')
    if removed > 0:
        parts.append(f'<span class="lines-removed">\u2212{int(removed)}</span>')
    return " / ".join(parts)


def generate_task_row(t: dict, criteria_list: list[dict], task_deps: dict, summary_map: dict, max_cost: float = 0, tool_stats: list[dict] = None) -> str:
    """Generate a single task table row (and optional criteria/tool-cost detail row)."""
    has_data = t["session_count"] > 0
    status_val = esc(t['status'])
    tid = t['id']
    has_criteria = len(criteria_list) > 0
    has_tool_stats = bool(tool_stats)
    has_expandable = has_criteria or has_tool_stats
    toggle_icon = '<span class="expand-icon">&#9654;</span> ' if has_expandable else ''

    row_classes = []
    if not has_data:
        row_classes.append('muted')
    if has_expandable:
        row_classes.append('expandable')
    cls_attr = f' class="{" ".join(row_classes)}"' if row_classes else ''

    priority_score = t.get('priority_score') or 0
    complexity_val = esc(t.get('complexity') or '')
    complexity_sort = COMPLEXITY_SORT_ORDER.get(t.get('complexity') or '', 0)
    domain_val = esc(t.get('domain') or '')
    task_type_val = esc(t.get('task_type') or '')
    session_count = t.get('session_count') or 0
    models_raw = t.get('models') or ''
    duration_seconds = t.get('total_duration_seconds') or 0
    lines_added = t.get('total_lines_added') or 0
    lines_removed = t.get('total_lines_removed') or 0
    total_lines = int(lines_added) + int(lines_removed)
    dep_badges = build_dep_badges(tid, task_deps, summary_map)
    summary_cell = f'<div class="summary-text">{esc(t["summary"])}</div>{dep_badges}'

    # Cost heatmap class for the cost cell
    heat_cls = cost_heat_class(t['total_cost'], max_cost)
    cost_cls = f'col-cost {heat_cls}'.strip()

    row = f"""<tr{cls_attr} data-status="{status_val}" data-summary="{esc(t['summary']).lower()}" data-task-id="{tid}" data-domain="{domain_val}" data-complexity="{complexity_val}" data-type="{task_type_val}">
  <td class="col-id" data-sort="{tid}">{toggle_icon}#{tid}</td>
  <td class="col-summary">{summary_cell}</td>
  <td class="col-status"><span class="status-badge status-{status_val.lower().replace(' ', '-')}">{status_val}</span></td>
  <td class="col-domain">{domain_val}</td>
  <td class="col-complexity" data-sort="{complexity_sort}">{f'<span class="complexity-badge">{complexity_val}</span>' if complexity_val else ''}</td>
  <td class="col-wsjf" data-sort="{priority_score}">{priority_score}</td>
  <td class="col-sessions" data-sort="{session_count}">{session_count if session_count else '<span class="text-muted-dash">&mdash;</span>'}</td>
  <td class="col-model" data-sort="{esc(models_raw)}" title="{esc(models_raw)}">{esc(models_raw) if models_raw else '<span class="text-muted-dash">&mdash;</span>'}</td>
  <td class="col-duration" data-sort="{duration_seconds}">{format_duration(duration_seconds) if duration_seconds else '<span class="text-muted-dash">&mdash;</span>'}</td>
  <td class="col-lines" data-sort="{total_lines}" data-lines-added="{int(lines_added)}" data-lines-removed="{int(lines_removed)}">{format_lines_html(lines_added, lines_removed)}</td>
  <td class="col-tokens-in" data-sort="{t['total_tokens_in']}">{format_tokens_compact(t['total_tokens_in'])}</td>
  <td class="col-tokens-out" data-sort="{t['total_tokens_out']}">{format_tokens_compact(t['total_tokens_out'])}</td>
  <td class="{cost_cls}" data-sort="{t['total_cost']}">{format_cost(t['total_cost'])}</td>
  <td class="col-updated" data-sort="{esc(t.get('updated_at') or '')}">{format_relative_time(t.get('updated_at'))}</td>
</tr>\n"""

    if has_expandable:
        row += generate_criteria_detail(tid, has_criteria=has_criteria, tool_stats=tool_stats)

    return row


def generate_criteria_detail(tid: int, has_criteria: bool = True, tool_stats: list[dict] = None) -> str:
    """Generate the collapsible detail row for a task.

    Contains an optional criteria panel (client-side rendered from JSON) and
    an optional tool cost breakdown panel (server-side rendered).
    """
    inner = ""

    if has_criteria:
        sort_bar = (
            '<div class="criteria-sort-bar">'
            '<div class="criteria-view-modes">'
            '<button class="criteria-view-btn active" data-view="commit">By Commit</button>'
            '<button class="criteria-view-btn" data-view="status">By Status</button>'
            '<button class="criteria-view-btn" data-view="flat">Flat</button>'
            '</div>'
            '<span class="criteria-sort-sep"></span>'
            '<span class="criteria-sort-label">Sort:</span>'
            '<button class="criteria-sort-btn" data-sort-key="completed">Completed <span class="sort-arrow">&#9650;</span></button>'
            '<button class="criteria-sort-btn" data-sort-key="cost">Cost <span class="sort-arrow">&#9650;</span></button>'
            '<button class="criteria-sort-btn" data-sort-key="commit">Commit <span class="sort-arrow">&#9650;</span></button>'
            '</div>'
        )
        inner += (
            f'<div class="criteria-detail" data-tid="{tid}">'
            f'{sort_bar}'
            f'<div class="criteria-render-target"></div>'
            f'</div>'
        )

    if tool_stats:
        inner += _generate_tool_stats_panel(tool_stats)

    return (
        f'<tr class="criteria-row" data-parent="{tid}" style="display:none">\n'
        f'  <td colspan="14">{inner}</td>\n'
        f'</tr>\n'
    )


def generate_table_footer(total_sessions: int, total_duration: int, total_lines_added: int,
                          total_lines_removed: int, total_tokens_in: int, total_tokens_out: int,
                          total_cost: float) -> str:
    """Generate the table footer with totals."""
    total_lines = int(total_lines_added) + int(total_lines_removed)
    return f"""\
<tfoot>
  <tr>
    <td colspan="6" id="footerLabel">Total</td>
    <td class="col-sessions" id="footerSessions">{total_sessions}</td>
    <td class="col-model"></td>
    <td class="col-duration" id="footerDuration">{format_duration(total_duration)}</td>
    <td class="col-lines" id="footerLines">{format_lines_html(total_lines_added, total_lines_removed)}</td>
    <td class="col-tokens-in" id="footerTokensIn">{format_tokens_compact(total_tokens_in)}</td>
    <td class="col-tokens-out" id="footerTokensOut">{format_tokens_compact(total_tokens_out)}</td>
    <td class="col-cost" id="footerCost">{format_cost(total_cost)}</td>
    <td></td>
  </tr>
</tfoot>"""


def generate_pagination() -> str:
    """Generate the pagination bar."""
    return """\
<div class="pagination-bar" id="paginationBar">
  <span class="page-info" id="pageInfo"></span>
  <div class="pagination-controls">
    <label>Per page:
      <select class="page-size-select" id="pageSize">
        <option value="25">25</option>
        <option value="50">50</option>
        <option value="0">All</option>
      </select>
    </label>
    <button class="page-btn" id="prevPage">\u2190 Prev</button>
    <button class="page-btn" id="nextPage">Next \u2192</button>
  </div>
</div>"""


def generate_complexity_section(complexity_metrics: list[dict] | None) -> str:
    """Generate the estimate vs. actual complexity section."""
    if not complexity_metrics:
        return ""

    complexity_rows = ""
    for c in complexity_metrics:
        tier = c['complexity']
        expected = EXPECTED_SESSIONS.get(tier, (0, 0))
        lo, hi = expected
        expected_str = f"{lo:.0f}&ndash;{hi:.0f}" if lo == int(lo) and hi == int(hi) else f"{lo}&ndash;{hi}"
        avg_sessions = c['avg_sessions'] or 0
        exceeds = avg_sessions > hi
        row_css = ' class="tier-exceeds"' if exceeds else ''
        flag = ' <span class="tier-flag">&#9888;</span>' if exceeds else ''
        complexity_rows += f"""<tr{row_css}>
  <td class="col-complexity"><span class="complexity-badge">{esc(tier)}</span></td>
  <td class="col-count">{c['task_count']}</td>
  <td class="col-expected">{expected_str}</td>
  <td class="col-avg-sessions">{c['avg_sessions']}{flag}</td>
  <td class="col-avg-duration">{format_duration(c['avg_duration_seconds'])}</td>
  <td class="col-avg-cost">{format_cost(c['avg_cost'])}</td>
</tr>\n"""

    return f"""
<div class="panel" style="margin-top: var(--sp-6);">
  <div class="section-header">Estimate vs. Actual</div>
  <table>
    <thead>
      <tr>
        <th>Complexity</th>
        <th style="text-align:right">Tasks</th>
        <th style="text-align:right">Expected Sessions</th>
        <th style="text-align:right">Avg Sessions</th>
        <th style="text-align:right">Avg Duration</th>
        <th style="text-align:right">Avg Cost</th>
      </tr>
    </thead>
    <tbody>
      {complexity_rows}
    </tbody>
  </table>
</div>"""


def generate_dag_section(dag_tasks: list[dict], edges: list[dict],
                         dag_blockers: list[dict]) -> str:
    """Generate the DAG tab panel HTML with Mermaid graph, sidebar, and legend."""
    # Build two versions: default (filtered) and all (with Done tasks)
    filtered_tasks, filtered_edges, filtered_blockers = filter_dag_nodes(
        dag_tasks, edges, dag_blockers, show_all=False
    )
    all_tasks, all_edges, all_blockers = filter_dag_nodes(
        dag_tasks, edges, dag_blockers, show_all=True
    )

    mermaid_default = build_mermaid(filtered_tasks, filtered_edges, filtered_blockers)
    mermaid_all = build_mermaid(all_tasks, all_edges, all_blockers)

    # Build task data JSON for sidebar
    task_data: dict[int, dict] = {}
    blockers_by_task: dict[int, list] = defaultdict(list)
    for b in dag_blockers:
        blockers_by_task[b["task_id"]].append({
            "id": b["id"],
            "description": b["description"],
            "blocker_type": b["blocker_type"],
            "is_resolved": b["is_resolved"],
        })

    for t in dag_tasks:
        tb = blockers_by_task.get(t["id"], [])
        task_data[t["id"]] = {
            "id": t["id"],
            "summary": t["summary"],
            "status": t["status"],
            "priority": t["priority"],
            "complexity": t["complexity"],
            "domain": t["domain"],
            "task_type": t["task_type"],
            "priority_score": t["priority_score"],
            "sessions": t["session_count"],
            "tokens_in": format_number(t["total_tokens_in"]),
            "tokens_out": format_number(t["total_tokens_out"]),
            "cost": format_cost(t["total_cost"]),
            "duration": format_duration(t["total_duration_seconds"]),
            "criteria_done": t["criteria_done"],
            "criteria_total": t["criteria_total"],
            "blockers": tb,
        }

    blocker_data: dict[int, dict] = {}
    for b in dag_blockers:
        blocker_data[b["id"]] = {
            "id": b["id"],
            "task_id": b["task_id"],
            "description": b["description"],
            "blocker_type": b["blocker_type"],
            "is_resolved": b["is_resolved"],
        }

    task_json = json.dumps(task_data).replace("</", "<\\/")
    blocker_json = json.dumps(blocker_data).replace("</", "<\\/")
    mermaid_default_json = json.dumps(mermaid_default).replace("</", "<\\/")
    mermaid_all_json = json.dumps(mermaid_all).replace("</", "<\\/")

    has_edges = len(edges) > 0 or len(dag_blockers) > 0
    hint = "" if has_edges else '<p class="dag-hint">No dependencies yet. Use <code>tusk deps add</code> to connect tasks.</p>'

    return f"""\
<script>
var DAG_TASK_DATA = {task_json};
var DAG_BLOCKER_DATA = {blocker_json};
var DAG_MERMAID_DEFAULT = {mermaid_default_json};
var DAG_MERMAID_ALL = {mermaid_all_json};
</script>
<div class="dag-toolbar">
  <label class="dag-toggle-label">
    <input type="checkbox" id="dagShowDone"> Show Done tasks
  </label>
</div>
<div class="dag-main">
  <div class="dag-graph-panel">
    <div id="dagMermaidContainer"></div>
    {hint}
    <div class="dag-legend">
      <div class="dag-legend-title">Legend</div>
      <div class="dag-legend-row">
        <span class="dag-legend-item"><span class="dag-legend-swatch" style="background:#3b82f6"></span> To Do</span>
        <span class="dag-legend-item"><span class="dag-legend-swatch" style="background:#f59e0b"></span> In Progress</span>
        <span class="dag-legend-item"><span class="dag-legend-swatch" style="background:#22c55e"></span> Done</span>
        <span class="dag-legend-item"><span class="dag-legend-swatch" style="background:#ef4444"></span> Blocker</span>
        <span class="dag-legend-item"><span class="dag-legend-swatch" style="background:#9ca3af"></span> Resolved</span>
      </div>
      <div class="dag-legend-row">
        <span class="dag-legend-item">[rect] = XS/S</span>
        <span class="dag-legend-item">(rounded) = M</span>
        <span class="dag-legend-item">&#x2B21; hexagon = L/XL</span>
        <span class="dag-legend-item">&#x25B7; flag = blocker</span>
      </div>
      <div class="dag-legend-row">
        <span class="dag-legend-item">&mdash;&mdash;&gt; blocks</span>
        <span class="dag-legend-item">- - -&gt; contingent</span>
        <span class="dag-legend-item">-&middot;-x blocker</span>
      </div>
    </div>
  </div>
  <div class="dag-sidebar">
    <div class="dag-sidebar-placeholder" id="dagPlaceholder">
      Click a node to inspect task details
    </div>
    <div class="dag-sidebar-content" id="dagSidebarContent">
      <h2 id="dagSbTitle"></h2>
      <div id="dagSbMetrics"></div>
    </div>
  </div>
</div>"""


def generate_js() -> str:
    """Generate all dashboard JavaScript."""
    return """\
<script>
(function() {
  var body = document.getElementById('metricsBody');
  if (!body) return;
  var allRows = Array.prototype.slice.call(body.querySelectorAll('tr:not(.criteria-row)'));
  var criteriaRows = {};
  body.querySelectorAll('tr.criteria-row').forEach(function(cr) {
    criteriaRows[cr.getAttribute('data-parent')] = cr;
  });
  var filtered = allRows.slice();
  var currentPage = 1;
  var pageSize = 25;
  var sortCol = 13;
  var sortAsc = false;
  var statusFilter = 'All';
  var searchTerm = '';
  var domainFilter = '';
  var complexityFilter = '';
  var typeFilter = '';

  var headers = document.querySelectorAll('#metricsTable thead th');
  var chips = document.querySelectorAll('#statusFilters .filter-chip');
  var searchInput = document.getElementById('searchInput');
  var domainSelect = document.getElementById('domainFilter');
  var complexitySelect = document.getElementById('complexityFilter');
  var typeSelect = document.getElementById('typeFilter');
  var filterBadge = document.getElementById('filterBadge');
  var clearBtn = document.getElementById('clearFilters');
  var pageSizeEl = document.getElementById('pageSize');
  var prevBtn = document.getElementById('prevPage');
  var nextBtn = document.getElementById('nextPage');
  var pageInfo = document.getElementById('pageInfo');
  var footerLabel = document.getElementById('footerLabel');
  var footerSessions = document.getElementById('footerSessions');
  var footerDuration = document.getElementById('footerDuration');
  var footerLines = document.getElementById('footerLines');
  var footerIn = document.getElementById('footerTokensIn');
  var footerOut = document.getElementById('footerTokensOut');
  var footerCost = document.getElementById('footerCost');

  // Populate dropdown options from row data
  function populateSelect(select, attr, placeholder) {
    var values = {};
    allRows.forEach(function(row) {
      var v = row.getAttribute(attr) || '';
      if (v) values[v] = true;
    });
    var sorted = Object.keys(values).sort();
    select.innerHTML = '<option value="">' + placeholder + '</option>';
    sorted.forEach(function(v) {
      var opt = document.createElement('option');
      opt.value = v;
      opt.textContent = v;
      select.appendChild(opt);
    });
  }

  var complexityOrder = ['XS', 'S', 'M', 'L', 'XL'];
  function populateComplexitySelect() {
    var values = {};
    allRows.forEach(function(row) {
      var v = row.getAttribute('data-complexity') || '';
      if (v) values[v] = true;
    });
    complexitySelect.innerHTML = '<option value="">Size</option>';
    complexityOrder.forEach(function(v) {
      if (values[v]) {
        var opt = document.createElement('option');
        opt.value = v;
        opt.textContent = v;
        complexitySelect.appendChild(opt);
      }
    });
  }

  populateSelect(domainSelect, 'data-domain', 'Domain');
  populateComplexitySelect();
  populateSelect(typeSelect, 'data-type', 'Type');

  // --- URL hash state ---
  var hashUpdateTimer = null;

  function encodeHashState() {
    var params = [];
    if (statusFilter !== 'All') params.push('s=' + encodeURIComponent(statusFilter));
    if (domainFilter) params.push('d=' + encodeURIComponent(domainFilter));
    if (complexityFilter) params.push('c=' + encodeURIComponent(complexityFilter));
    if (typeFilter) params.push('t=' + encodeURIComponent(typeFilter));
    if (searchTerm) params.push('q=' + encodeURIComponent(searchTerm));
    if (sortCol !== 13) params.push('sc=' + sortCol);
    if (sortAsc) params.push('sa=1');
    if (currentPage !== 1) params.push('p=' + currentPage);
    if (pageSize !== 25) params.push('ps=' + pageSize);
    return params.length > 0 ? params.join('&') : '';
  }

  function pushHashState() {
    if (hashUpdateTimer) clearTimeout(hashUpdateTimer);
    hashUpdateTimer = setTimeout(function() {
      var hash = encodeHashState();
      var newUrl = window.location.pathname + (hash ? '#' + hash : '');
      history.replaceState(null, '', newUrl);
    }, 100);
  }

  function restoreHashState() {
    var hash = window.location.hash.replace(/^#/, '');
    if (!hash) return false;
    var pairs = hash.split('&');
    var restored = false;
    pairs.forEach(function(pair) {
      var kv = pair.split('=');
      var k = kv[0];
      var v = decodeURIComponent(kv.slice(1).join('='));
      switch (k) {
        case 's': statusFilter = v; restored = true; break;
        case 'd': domainFilter = v; restored = true; break;
        case 'c': complexityFilter = v; restored = true; break;
        case 't': typeFilter = v; restored = true; break;
        case 'q': searchTerm = v; restored = true; break;
        case 'sc': sortCol = parseInt(v) || 13; restored = true; break;
        case 'sa': sortAsc = v === '1'; restored = true; break;
        case 'p': currentPage = parseInt(v) || 1; restored = true; break;
        case 'ps': pageSize = parseInt(v) || 25; restored = true; break;
      }
    });
    return restored;
  }

  function syncUIFromState() {
    // Status chips
    chips.forEach(function(c) {
      c.classList.toggle('active', c.getAttribute('data-filter') === statusFilter);
    });
    // Dropdowns
    domainSelect.value = domainFilter;
    complexitySelect.value = complexityFilter;
    typeSelect.value = typeFilter;
    // Search
    searchInput.value = searchTerm;
    // Page size
    pageSizeEl.value = pageSize.toString();
    // Sort header highlight
    headers.forEach(function(h) {
      h.classList.remove('sort-asc', 'sort-desc');
      h.querySelector('.sort-arrow').textContent = '\\u25B2';
    });
    if (sortCol >= 0 && sortCol < headers.length) {
      headers[sortCol].classList.add(sortAsc ? 'sort-asc' : 'sort-desc');
      headers[sortCol].querySelector('.sort-arrow').textContent = sortAsc ? '\\u25B2' : '\\u25BC';
    }
  }

  // --- Active filter badge ---
  function updateFilterBadge() {
    var count = 0;
    if (statusFilter !== 'All') count++;
    if (domainFilter) count++;
    if (complexityFilter) count++;
    if (typeFilter) count++;
    if (searchTerm) count++;
    if (count > 0) {
      filterBadge.textContent = count;
      filterBadge.classList.remove('hidden');
      clearBtn.classList.remove('hidden');
    } else {
      filterBadge.classList.add('hidden');
      clearBtn.classList.add('hidden');
    }
  }

  function clearAllFilters() {
    statusFilter = 'All';
    domainFilter = '';
    complexityFilter = '';
    typeFilter = '';
    searchTerm = '';
    syncUIFromState();
    applyFilter();
  }

  function formatCost(n) {
    return '$' + n.toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',');
  }

  function formatTokensCompact(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return Math.round(n).toString();
  }

  function formatDuration(seconds) {
    if (!seconds || seconds <= 0) return '0m';
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm';
  }

  function formatLinesHtml(totalLines) {
    // We only have the total for filtering; full HTML comes from server
    return totalLines > 0 ? totalLines.toString() : '\\u2014';
  }

  function applyFilter() {
    filtered = allRows.filter(function(row) {
      if (statusFilter !== 'All' && row.getAttribute('data-status') !== statusFilter) return false;
      if (domainFilter && row.getAttribute('data-domain') !== domainFilter) return false;
      if (complexityFilter && row.getAttribute('data-complexity') !== complexityFilter) return false;
      if (typeFilter && row.getAttribute('data-type') !== typeFilter) return false;
      if (searchTerm && row.getAttribute('data-summary').indexOf(searchTerm) === -1) return false;
      return true;
    });
    currentPage = 1;
    updateFilterBadge();
    pushHashState();
    render();
  }

  function applySort() {
    if (sortCol < 0) return;
    var type = headers[sortCol].getAttribute('data-type');
    filtered.sort(function(a, b) {
      var cellA = a.children[sortCol];
      var cellB = b.children[sortCol];
      var vA, vB;
      if (type === 'num') {
        vA = parseFloat(cellA.getAttribute('data-sort')) || 0;
        vB = parseFloat(cellB.getAttribute('data-sort')) || 0;
      } else {
        vA = (cellA.getAttribute('data-sort') || cellA.textContent || '').toLowerCase();
        vB = (cellB.getAttribute('data-sort') || cellB.textContent || '').toLowerCase();
      }
      if (vA < vB) return sortAsc ? -1 : 1;
      if (vA > vB) return sortAsc ? 1 : -1;
      return 0;
    });
    pushHashState();
    render();
  }

  function isFiltered() {
    return statusFilter !== 'All' || domainFilter || complexityFilter || typeFilter || searchTerm;
  }

  function updateFooter() {
    var totalSessions = 0, totalDuration = 0;
    var totalLinesAdded = 0, totalLinesRemoved = 0;
    var totalIn = 0, totalOut = 0, totalCost = 0, count = 0;
    filtered.forEach(function(row) {
      totalSessions += parseFloat(row.children[6].getAttribute('data-sort')) || 0;
      totalDuration += parseFloat(row.children[7].getAttribute('data-sort')) || 0;
      totalLinesAdded += parseFloat(row.children[8].getAttribute('data-lines-added')) || 0;
      totalLinesRemoved += parseFloat(row.children[8].getAttribute('data-lines-removed')) || 0;
      totalIn += parseFloat(row.children[9].getAttribute('data-sort')) || 0;
      totalOut += parseFloat(row.children[10].getAttribute('data-sort')) || 0;
      totalCost += parseFloat(row.children[11].getAttribute('data-sort')) || 0;
      count++;
    });
    var label = isFiltered() ? 'Filtered total (' + count + ' tasks)' : 'Total';
    footerLabel.textContent = label;
    footerSessions.textContent = totalSessions;
    footerDuration.textContent = formatDuration(totalDuration);
    var linesParts = [];
    if (totalLinesAdded > 0) linesParts.push('<span class="lines-added">+' + totalLinesAdded + '</span>');
    if (totalLinesRemoved > 0) linesParts.push('<span class="lines-removed">\u2212' + totalLinesRemoved + '</span>');
    footerLines.innerHTML = linesParts.length > 0 ? linesParts.join(' / ') : '\u2014';
    footerIn.textContent = formatTokensCompact(totalIn);
    footerOut.textContent = formatTokensCompact(totalOut);
    footerCost.textContent = formatCost(totalCost);
  }

  function render() {
    allRows.forEach(function(r) { r.style.display = 'none'; });
    Object.keys(criteriaRows).forEach(function(k) { criteriaRows[k].style.display = 'none'; });

    var start, end;
    if (pageSize === 0) {
      start = 0;
      end = filtered.length;
    } else {
      var maxPage = Math.max(1, Math.ceil(filtered.length / pageSize));
      if (currentPage > maxPage) currentPage = maxPage;
      start = (currentPage - 1) * pageSize;
      end = Math.min(start + pageSize, filtered.length);
    }

    for (var i = 0; i < filtered.length; i++) {
      body.appendChild(filtered[i]);
      var tid = filtered[i].getAttribute('data-task-id');
      if (tid && criteriaRows[tid]) {
        body.appendChild(criteriaRows[tid]);
      }
    }
    for (var j = start; j < end; j++) {
      filtered[j].style.display = '';
      var jtid = filtered[j].getAttribute('data-task-id');
      if (jtid && criteriaRows[jtid] && filtered[j].classList.contains('expanded')) {
        criteriaRows[jtid].style.display = '';
      }
    }

    if (pageSize === 0) {
      pageInfo.textContent = filtered.length + ' tasks';
      prevBtn.disabled = true;
      nextBtn.disabled = true;
    } else {
      var maxP = Math.max(1, Math.ceil(filtered.length / pageSize));
      pageInfo.textContent = 'Page ' + currentPage + ' of ' + maxP + ' (' + filtered.length + ' tasks)';
      prevBtn.disabled = currentPage <= 1;
      nextBtn.disabled = currentPage >= maxP;
    }

    updateFooter();
  }

  // --- Criteria client-side rendering engine ---
  var CDATA = window.CRITERIA_DATA || {};
  var criteriaRendered = {};

  function escHtml(s) {
    if (s == null) return '';
    var d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  }

  function fmtDate(s) {
    if (!s) return '';
    return s.replace(/\.\d+$/, '');
  }

  function renderCriterionToolPanel(toolStats) {
    if (!toolStats || toolStats.length === 0) return '';
    var total = 0;
    toolStats.forEach(function(t) { total += t.total_cost || 0; });
    var rows = '';
    toolStats.forEach(function(t) {
      var cost = t.total_cost || 0;
      var pct = total > 0 ? (cost / total * 100) : 0;
      rows += '<tr class="tc-row">'
        + '<td class="tc-tool">' + escHtml(t.tool_name) + '</td>'
        + '<td class="tc-calls" style="text-align:right;font-variant-numeric:tabular-nums;">' + (t.call_count || 0).toLocaleString() + '</td>'
        + '<td class="tc-cost" style="text-align:right;font-variant-numeric:tabular-nums;">$' + cost.toFixed(4) + '</td>'
        + '<td class="tc-pct" style="min-width:100px;">'
        + '<div style="display:flex;align-items:center;gap:6px;">'
        + '<div style="flex:1;background:var(--border);border-radius:3px;height:8px;overflow:hidden;">'
        + '<div style="width:' + pct.toFixed(1) + '%;background:var(--accent,#3b82f6);height:100%;border-radius:3px;"></div>'
        + '</div>'
        + '<span style="font-size:0.75rem;color:var(--text-muted,#6b7280);min-width:36px;">' + pct.toFixed(1) + '%</span>'
        + '</div></td>'
        + '</tr>\n';
    });
    return '<details class="tc-task-panel tc-task-panel--bordered" style="margin-top:4px;">'
      + '<summary style="padding:4px 8px;cursor:pointer;list-style:none;'
      + 'display:flex;justify-content:space-between;align-items:center;'
      + 'font-size:0.8rem;color:var(--text-muted,#6b7280);">'
      + '<span>Tool-attributed cost</span>'
      + '<span style="font-variant-numeric:tabular-nums;" title="Sum of per-tool attributed costs — may differ from criterion\'s cost_dollars">$' + total.toFixed(4) + '</span>'
      + '</summary>'
      + '<div style="overflow-x:auto;padding:0 8px 6px;">'
      + '<table class="tc-table" style="margin-top:0;width:100%;">'
      + '<thead><tr><th>Tool</th><th style="text-align:right">Calls</th>'
      + '<th style="text-align:right">Cost</th><th>Share</th></tr></thead>'
      + '<tbody>' + rows + '</tbody>'
      + '</table></div></details>';
  }

  function renderCriterionItem(cr, repoUrl) {
    var done = cr.is_completed;
    var css = done ? 'criterion-done' : 'criterion-pending';
    var check = done ? '&#10003;' : '&#9711;';
    var ctype = cr.criterion_type || 'manual';
    var badges = '<span class="criterion-badges">';
    badges += '<span class="criterion-type criterion-type-' + escHtml(ctype) + '">' + escHtml(ctype) + '</span>';
    if (cr.source) badges += ' <span class="criterion-source">' + escHtml(cr.source) + '</span>';
    if (cr.cost_dollars) badges += ' <span class="criterion-cost">$' + cr.cost_dollars.toFixed(4) + '</span>';
    if (cr.commit_hash) {
      if (repoUrl) {
        badges += ' <a href="' + repoUrl + '/commit/' + escHtml(cr.commit_hash) + '" class="criterion-commit" target="_blank">' + escHtml(cr.commit_hash) + '</a>';
      } else {
        badges += ' <span class="criterion-commit">' + escHtml(cr.commit_hash) + '</span>';
      }
    }
    if (cr.completed_at) badges += ' <span class="criterion-time">' + fmtDate(cr.completed_at) + '</span>';
    badges += '</span>';

    var toolPanel = renderCriterionToolPanel(cr.tool_stats);

    return '<div class="criterion-item ' + css + '" data-sort-completed="' + escHtml(cr.completed_at || '') + '" '
      + 'data-sort-cost="' + (cr.cost_dollars || 0) + '" data-sort-commit="' + escHtml(cr.commit_hash || '') + '" data-cid="' + cr.id + '">'
      + '<span class="criterion-id">#' + cr.id + '</span>'
      + '<span class="criterion-status">' + check + '</span>'
      + '<span class="criterion-text">' + escHtml(cr.criterion) + '</span>'
      + badges + toolPanel + '</div>';
  }

  function renderGroupHeader(label, labelHtml, done, total, cost, tokens) {
    var costBadge = cost ? ' <span class="criteria-group-cost">$' + cost.toFixed(4) + '</span>' : '';
    var tokenBadge = tokens ? ' <span class="criteria-group-tokens">' + tokens.toLocaleString() + ' tok</span>' : '';
    var pct = total > 0 ? Math.round(done / total * 100) : 0;
    return '<div class="criteria-group-header"><span class="criteria-group-icon">&#9654;</span> '
      + labelHtml + ' &mdash; <span class="criteria-group-count">' + done + '/' + total + ' done</span>'
      + costBadge + tokenBadge + '</div>'
      + '<div class="criteria-group-progress"><div class="criteria-group-progress-fill" style="width:' + pct + '%"></div></div>';
  }

  function buildGroup(groupKey, labelHtml, items, repoUrl) {
    var done = 0, total = items.length, cost = 0, tokens = 0;
    items.forEach(function(cr) {
      if (cr.is_completed) done++;
      cost += cr.cost_dollars || 0;
      tokens += (cr.tokens_in || 0) + (cr.tokens_out || 0);
    });
    var allDone = done === total ? ' criteria-group-all-done' : '';
    var html = '<div class="criteria-type-group' + allDone + '" data-group-type="' + escHtml(groupKey) + '">';
    html += renderGroupHeader(groupKey, labelHtml, done, total, cost, tokens);
    html += '<div class="criteria-group-items">';
    items.forEach(function(cr) { html += renderCriterionItem(cr, repoUrl); });
    html += '</div></div>';
    return html;
  }

  function renderByCommit(taskData) {
    var criteria = taskData.criteria;
    var repoUrl = taskData.repo_url || '';
    var groups = {};
    var timestamps = {};
    criteria.forEach(function(cr) {
      var h = cr.commit_hash || null;
      var key = h || '__uncommitted__';
      if (!groups[key]) groups[key] = [];
      groups[key].push(cr);
      if (h && cr.committed_at && !timestamps[key]) timestamps[key] = cr.committed_at;
    });
    var committed = Object.keys(groups).filter(function(k) { return k !== '__uncommitted__'; });
    committed.sort(function(a, b) { return (timestamps[b] || '').localeCompare(timestamps[a] || ''); });
    var order = committed.slice();
    if (groups['__uncommitted__']) order.push('__uncommitted__');

    var html = '';
    order.forEach(function(key) {
      var labelHtml;
      if (key === '__uncommitted__') {
        labelHtml = '<span class="criteria-group-name">Uncommitted</span>';
      } else {
        var short = escHtml(key.substring(0, 8));
        var ts = fmtDate(timestamps[key] || '');
        if (repoUrl) {
          labelHtml = '<a href="' + repoUrl + '/commit/' + escHtml(key) + '" class="criteria-group-commit-link" target="_blank">' + short + '</a>';
        } else {
          labelHtml = '<span class="criteria-group-commit-hash">' + short + '</span>';
        }
        if (ts) labelHtml += ' <span class="criteria-group-time">' + ts + '</span>';
      }
      html += buildGroup(key, labelHtml, groups[key], repoUrl);
    });
    return html;
  }

  function renderByStatus(taskData) {
    var criteria = taskData.criteria;
    var repoUrl = taskData.repo_url || '';
    var done = [], pending = [];
    criteria.forEach(function(cr) {
      if (cr.is_completed) done.push(cr); else pending.push(cr);
    });
    var html = '';
    if (pending.length) {
      html += buildGroup('pending', '<span class="criteria-group-name">Pending</span>', pending, repoUrl);
    }
    if (done.length) {
      html += buildGroup('done', '<span class="criteria-group-name">Completed</span>', done, repoUrl);
    }
    return html;
  }

  function renderFlat(taskData) {
    var repoUrl = taskData.repo_url || '';
    var html = '';
    taskData.criteria.forEach(function(cr) { html += renderCriterionItem(cr, repoUrl); });
    return html;
  }

  function renderCriteria(detail, viewMode) {
    var tid = detail.getAttribute('data-tid');
    var taskData = CDATA[tid];
    if (!taskData) return;
    var target = detail.querySelector('.criteria-render-target');
    if (viewMode === 'commit') {
      target.innerHTML = renderByCommit(taskData);
    } else if (viewMode === 'status') {
      target.innerHTML = renderByStatus(taskData);
    } else {
      target.innerHTML = renderFlat(taskData);
    }
    // Re-apply sort if active
    var activeSort = detail.querySelector('.criteria-sort-btn.sort-asc, .criteria-sort-btn.sort-desc');
    if (activeSort) {
      applyCriteriaSort(detail, activeSort.getAttribute('data-sort-key'),
        activeSort.classList.contains('sort-asc') ? 'asc' : 'desc');
    }
  }

  function getActiveView(detail) {
    var activeBtn = detail.querySelector('.criteria-view-btn.active');
    return activeBtn ? activeBtn.getAttribute('data-view') : 'commit';
  }

  function applyCriteriaSort(detail, sortKey, dir) {
    function sortItems(container) {
      var items = Array.prototype.slice.call(container.querySelectorAll(':scope > .criterion-item'));
      if (dir === 'none') {
        items.sort(function(a, b) { return parseInt(a.getAttribute('data-cid')) - parseInt(b.getAttribute('data-cid')); });
      } else {
        var attrName = 'data-sort-' + sortKey;
        var isNumeric = (sortKey === 'cost');
        items.sort(function(a, b) {
          var vA = a.getAttribute(attrName) || '';
          var vB = b.getAttribute(attrName) || '';
          var cmp = isNumeric ? ((parseFloat(vA) || 0) - (parseFloat(vB) || 0)) : vA.localeCompare(vB);
          return dir === 'asc' ? cmp : -cmp;
        });
      }
      items.forEach(function(item) { container.appendChild(item); });
    }
    detail.querySelectorAll('.criteria-group-items').forEach(function(gc) { sortItems(gc); });
    var flat = detail.querySelector('.criteria-render-target');
    if (flat && !detail.querySelector('.criteria-type-group')) { sortItems(flat); }
  }

  // Expand/collapse criteria rows — render on first expand
  body.addEventListener('click', function(e) {
    var row = e.target.closest('tr.expandable');
    if (!row) return;
    var tid = row.getAttribute('data-task-id');
    var detail = body.querySelector('tr.criteria-row[data-parent="' + tid + '"]');
    if (!detail) return;
    var isExpanded = row.classList.toggle('expanded');
    detail.style.display = isExpanded ? '' : 'none';
    if (isExpanded && !criteriaRendered[tid]) {
      var cd = detail.querySelector('.criteria-detail');
      if (cd) renderCriteria(cd, getActiveView(cd));
      criteriaRendered[tid] = true;
    }
  });

  // Criteria view mode buttons
  document.addEventListener('click', function(e) {
    var btn = e.target.closest('.criteria-view-btn');
    if (!btn) return;
    e.stopPropagation();
    var detail = btn.closest('.criteria-detail');
    if (!detail) return;
    detail.querySelectorAll('.criteria-view-btn').forEach(function(b) { b.classList.remove('active'); });
    btn.classList.add('active');
    renderCriteria(detail, btn.getAttribute('data-view'));
  });

  // Criteria group header collapse/expand
  document.addEventListener('click', function(e) {
    var header = e.target.closest('.criteria-group-header');
    if (!header) return;
    e.stopPropagation();
    var group = header.closest('.criteria-type-group');
    if (!group) return;
    group.classList.toggle('collapsed');
  });

  // Criteria sort buttons
  document.addEventListener('click', function(e) {
    var btn = e.target.closest('.criteria-sort-btn');
    if (!btn) return;
    e.stopPropagation();
    var detail = btn.closest('.criteria-detail');
    if (!detail) return;
    var bar = btn.closest('.criteria-sort-bar');
    var siblings = bar.querySelectorAll('.criteria-sort-btn');
    var wasAsc = btn.classList.contains('sort-asc');
    var wasDesc = btn.classList.contains('sort-desc');

    siblings.forEach(function(s) {
      s.classList.remove('sort-asc', 'sort-desc');
      s.querySelector('.sort-arrow').textContent = '\u25B2';
    });

    var dir;
    if (!wasAsc && !wasDesc) { dir = 'asc'; }
    else if (wasAsc) { dir = 'desc'; }
    else { dir = 'none'; }

    if (dir !== 'none') {
      btn.classList.add(dir === 'asc' ? 'sort-asc' : 'sort-desc');
      btn.querySelector('.sort-arrow').textContent = dir === 'asc' ? '\u25B2' : '\u25BC';
    }
    applyCriteriaSort(detail, btn.getAttribute('data-sort-key'), dir);
  });

  // Sort headers
  headers.forEach(function(th) {
    th.addEventListener('click', function() {
      var col = parseInt(th.getAttribute('data-col'));
      if (sortCol === col) {
        sortAsc = !sortAsc;
      } else {
        sortCol = col;
        sortAsc = true;
      }
      headers.forEach(function(h) {
        h.classList.remove('sort-asc', 'sort-desc');
        h.querySelector('.sort-arrow').textContent = '\u25B2';
      });
      th.classList.add(sortAsc ? 'sort-asc' : 'sort-desc');
      th.querySelector('.sort-arrow').textContent = sortAsc ? '\u25B2' : '\u25BC';
      applySort();
    });
  });

  // Status filter chips
  chips.forEach(function(chip) {
    chip.addEventListener('click', function() {
      chips.forEach(function(c) { c.classList.remove('active'); });
      chip.classList.add('active');
      statusFilter = chip.getAttribute('data-filter');
      applyFilter();
    });
  });

  // Dropdown filters
  domainSelect.addEventListener('change', function() {
    domainFilter = domainSelect.value;
    applyFilter();
  });
  complexitySelect.addEventListener('change', function() {
    complexityFilter = complexitySelect.value;
    applyFilter();
  });
  typeSelect.addEventListener('change', function() {
    typeFilter = typeSelect.value;
    applyFilter();
  });

  // Search input
  searchInput.addEventListener('input', function() {
    searchTerm = searchInput.value.toLowerCase();
    applyFilter();
  });

  // Clear all filters
  clearBtn.addEventListener('click', function() {
    clearAllFilters();
  });

  // Page size
  pageSizeEl.addEventListener('change', function() {
    pageSize = parseInt(pageSizeEl.value);
    currentPage = 1;
    pushHashState();
    render();
  });

  // Prev/Next
  prevBtn.addEventListener('click', function() {
    if (currentPage > 1) { currentPage--; pushHashState(); render(); }
  });
  nextBtn.addEventListener('click', function() {
    var maxP = Math.ceil(filtered.length / pageSize);
    if (currentPage < maxP) { currentPage++; pushHashState(); render(); }
  });

  // Restore state from URL hash, then initial render
  var restored = restoreHashState();
  if (restored) {
    syncUIFromState();
    updateFilterBadge();
  }
  applyFilter();
  applySort();

  // Chart.js initialization (graceful fallback if CDN unavailable)
  var costTrendChart = null;
  var domainChart = null;
  var currentPeriod = 'weekly';

  function initCharts() {
    if (typeof Chart === 'undefined') return;

    var style = getComputedStyle(document.documentElement);
    function cssVar(name) { return style.getPropertyValue(name).trim(); }

    // Trend chart
    if (window.__tuskCostTrend) {
      var trendData = window.__tuskCostTrend;
      var costTrendCanvas = document.getElementById('costTrendChart');
      var periodLabels = { daily: 'Daily', weekly: 'Weekly', monthly: 'Monthly' };

      if (costTrendChart) { costTrendChart.destroy(); costTrendChart = null; }

      var d = trendData[currentPeriod];
      if (d && d.costs.length && costTrendCanvas) {
        var accent = cssVar('--accent') || '#3b82f6';
        var warning = cssVar('--warning') || '#f59e0b';
        var textMuted = cssVar('--text-muted') || '#94a3b8';
        var border = cssVar('--border') || '#e2e8f0';
        costTrendChart = new Chart(costTrendCanvas, {
          type: 'bar',
          data: {
            labels: d.labels,
            datasets: [
              {
                label: periodLabels[currentPeriod] + ' Cost',
                data: d.costs,
                backgroundColor: accent + 'B3',
                borderColor: accent,
                borderWidth: 1,
                borderRadius: 2,
                yAxisID: 'y',
                order: 2
              },
              {
                label: 'Cumulative',
                data: d.cumulative,
                type: 'line',
                borderColor: warning,
                backgroundColor: warning + '33',
                pointBackgroundColor: warning,
                pointBorderColor: cssVar('--bg-panel') || '#ffffff',
                pointBorderWidth: 1.5,
                pointRadius: 3.5,
                borderWidth: 2.5,
                fill: false,
                tension: 0.1,
                yAxisID: 'y1',
                order: 1
              }
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
              tooltip: {
                callbacks: {
                  label: function(ctx) {
                    return ctx.dataset.label + ': $' + ctx.parsed.y.toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',');
                  }
                }
              },
              legend: {
                labels: { color: textMuted, usePointStyle: true, padding: 16 }
              }
            },
            scales: {
              x: {
                ticks: { color: textMuted, maxRotation: 45, autoSkip: true, maxTicksLimit: 12, font: { size: 11 } },
                grid: { display: false }
              },
              y: {
                position: 'left',
                ticks: {
                  color: textMuted,
                  font: { size: 11 },
                  callback: function(v) { return '$' + v.toFixed(0).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ','); }
                },
                grid: { color: border, borderDash: [3, 3] }
              },
              y1: {
                position: 'right',
                ticks: {
                  color: warning,
                  font: { size: 11 },
                  callback: function(v) { return '$' + v.toFixed(0).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ','); }
                },
                grid: { drawOnChartArea: false }
              }
            }
          }
        });
      }
    }

    // Cost by domain chart
    var domainData = window.__tuskCostByDomain;
    var domainCanvas = document.getElementById('costByDomainChart');
    if (domainCanvas && domainData && domainData.length > 0) {
      if (domainChart) { domainChart.destroy(); domainChart = null; }
      var domainLabels = domainData.map(function(d) { return d.domain || 'unset'; });
      var domainCosts = domainData.map(function(d) { return d.domain_cost; });
      var domainCounts = domainData.map(function(d) { return d.task_count; });
      var domainColors = domainData.map(function(_, i) {
        var hue = (i * 137.5) % 360;
        return 'hsl(' + hue + ', 65%, 55%)';
      });
      var style2 = getComputedStyle(document.documentElement);
      domainChart = new Chart(domainCanvas, {
        type: 'bar',
        data: {
          labels: domainLabels,
          datasets: [{
            label: 'Cost ($)',
            data: domainCosts,
            backgroundColor: domainColors.map(function(c) { return c.replace('55%)', '55%, 0.7)').replace('hsl(', 'hsla('); }),
            borderColor: domainColors,
            borderWidth: 1,
            borderRadius: 2
          }]
        },
        options: {
          indexAxis: 'y',
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            tooltip: {
              callbacks: {
                label: function(ctx) {
                  var cost = '$' + ctx.parsed.x.toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',');
                  var count = domainCounts[ctx.dataIndex];
                  return cost + ' (' + count + ' task' + (count !== 1 ? 's' : '') + ')';
                }
              }
            },
            legend: { display: false }
          },
          scales: {
            x: {
              ticks: {
                color: style2.getPropertyValue('--text-muted').trim() || '#94a3b8',
                font: { size: 11 },
                callback: function(v) { return '$' + v.toFixed(0).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ','); }
              },
              grid: { color: style2.getPropertyValue('--border').trim() || '#e2e8f0', borderDash: [3, 3] }
            },
            y: {
              ticks: { color: style2.getPropertyValue('--text-muted').trim() || '#94a3b8', font: { size: 12 } },
              grid: { display: false }
            }
          }
        }
      });
    }
  }

  initCharts();

  var costTabs = document.querySelectorAll('#costTrendTabs .cost-tab');
  costTabs.forEach(function(tab) {
    tab.addEventListener('click', function() {
      var target = tab.getAttribute('data-tab');
      costTabs.forEach(function(t) { t.classList.remove('active'); });
      tab.classList.add('active');
      currentPeriod = target;
      initCharts();
    });
  });

  // Theme toggle
  var themeToggle = document.getElementById('themeToggle');
  if (themeToggle) {
    themeToggle.addEventListener('click', function() {
      var html = document.documentElement;
      var current = html.getAttribute('data-theme');
      var next = current === 'dark' ? 'light' : 'dark';
      html.setAttribute('data-theme', next);
      localStorage.setItem('tusk-theme', next);
      // Re-render charts with new theme colors
      setTimeout(function() { initCharts(); }, 50);
    });
  }

  // Dependency badge click-to-scroll
  document.addEventListener('click', function(e) {
    var link = e.target.closest('.dep-link');
    if (!link) return;
    e.preventDefault();
    e.stopPropagation();
    var targetId = link.getAttribute('data-target');
    var targetRow = document.querySelector('tr[data-task-id="' + targetId + '"]');
    if (!targetRow) return;
    if (targetRow.style.display === 'none') {
      clearAllFilters();
    }
    targetRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
    targetRow.classList.add('dep-highlight');
    setTimeout(function() { targetRow.classList.remove('dep-highlight'); }, 2000);
  });

  // --- Tab navigation ---
  var tabBtns = document.querySelectorAll('#tabBar .tab-btn');
  var tabPanels = document.querySelectorAll('.tab-panel');

  function switchTab(tabId) {
    tabBtns.forEach(function(b) {
      b.classList.toggle('active', b.getAttribute('data-tab') === tabId);
    });
    tabPanels.forEach(function(p) {
      p.classList.toggle('active', p.id === 'tab-' + tabId);
    });
    // Render DAG on first switch to dag tab
    if (tabId === 'dag' && !window.__dagRendered) {
      window.__dagRendered = true;
      renderDag();
    }
  }

  tabBtns.forEach(function(btn) {
    btn.addEventListener('click', function() {
      var tab = btn.getAttribute('data-tab');
      switchTab(tab);
      // Update URL hash with tab parameter
      var hash = window.location.hash.replace(/^#/, '');
      var pairs = hash ? hash.split('&').filter(function(p) { return p.indexOf('tab=') !== 0; }) : [];
      if (tab !== 'dashboard') pairs.unshift('tab=' + tab);
      var newHash = pairs.join('&');
      history.replaceState(null, '', window.location.pathname + (newHash ? '#' + newHash : ''));
    });
  });

  // Restore tab from URL hash
  (function() {
    var hash = window.location.hash.replace(/^#/, '');
    if (!hash) return;
    var pairs = hash.split('&');
    for (var i = 0; i < pairs.length; i++) {
      var kv = pairs[i].split('=');
      if (kv[0] === 'tab' && kv[1]) {
        switchTab(kv[1]);
        return;
      }
    }
  })();

  // --- DAG rendering ---
  var dagRenderCount = 0;

  function renderDag() {
    if (typeof mermaid === 'undefined') return;
    var showDone = document.getElementById('dagShowDone');
    var def = (showDone && showDone.checked) ? window.DAG_MERMAID_ALL : window.DAG_MERMAID_DEFAULT;
    if (!def) return;
    var container = document.getElementById('dagMermaidContainer');
    if (!container) return;
    dagRenderCount++;
    var graphId = 'dagGraph' + dagRenderCount;
    mermaid.render(graphId, def).then(function(result) {
      container.innerHTML = result.svg;
    }).catch(function(err) {
      console.error('Mermaid render error:', err);
      container.innerHTML = '<p style="color:var(--danger);padding:1rem;">Failed to render DAG. Check console for details.</p>';
    });
  }

  // Show Done toggle
  var dagShowDone = document.getElementById('dagShowDone');
  if (dagShowDone) {
    dagShowDone.addEventListener('change', function() {
      renderDag();
    });
  }

  // --- DAG sidebar functions (global for Mermaid click callbacks) ---
  window.dagShowSidebar = function(nodeId) {
    var id = parseInt(nodeId.replace('T', ''), 10);
    var t = (window.DAG_TASK_DATA || {})[id];
    if (!t) return;

    document.getElementById('dagPlaceholder').style.display = 'none';
    var content = document.getElementById('dagSidebarContent');
    content.classList.add('active');
    document.getElementById('dagSbTitle').textContent = '#' + t.id + ': ' + t.summary;

    var statusMap = {'To Do': 'todo', 'In Progress': 'in-progress', 'Done': 'done'};
    var statusClass = 'status-' + (statusMap[t.status] || 'todo');
    var criteria = t.criteria_total > 0 ? t.criteria_done + '/' + t.criteria_total : '\\u2014';

    var m = '';
    m += '<div class="dag-metric"><span class="dag-metric-label">Status</span><span class="dag-metric-value"><span class="status-badge ' + statusClass + '">' + t.status + '</span></span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Priority</span><span class="dag-metric-value">' + (t.priority || '\\u2014') + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Complexity</span><span class="dag-metric-value">' + (t.complexity || '\\u2014') + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Domain</span><span class="dag-metric-value">' + (t.domain || '\\u2014') + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Type</span><span class="dag-metric-value">' + (t.task_type || '\\u2014') + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Priority Score</span><span class="dag-metric-value">' + (t.priority_score != null ? t.priority_score : '\\u2014') + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Sessions</span><span class="dag-metric-value">' + t.sessions + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Tokens In</span><span class="dag-metric-value">' + t.tokens_in + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Tokens Out</span><span class="dag-metric-value">' + t.tokens_out + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Cost</span><span class="dag-metric-value">' + t.cost + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Duration</span><span class="dag-metric-value">' + t.duration + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Criteria</span><span class="dag-metric-value">' + criteria + '</span></div>';

    if (t.blockers && t.blockers.length > 0) {
      m += '<div style="margin-top:0.75rem;font-weight:700;font-size:0.85rem;">External Blockers</div>';
      for (var i = 0; i < t.blockers.length; i++) {
        var b = t.blockers[i];
        var badge = b.is_resolved
          ? '<span class="dag-blocker-badge dag-blocker-resolved">Resolved</span>'
          : '<span class="dag-blocker-badge dag-blocker-open">Open</span>';
        m += '<div class="dag-blocker-item"><div class="dag-blocker-header">' + badge + ' <span class="dag-blocker-type">' + (b.blocker_type || 'external') + '</span></div><div class="dag-blocker-desc">' + b.description + '</div></div>';
      }
    }

    document.getElementById('dagSbMetrics').innerHTML = m;
  };

  window.dagShowBlockerSidebar = function(nodeId) {
    var id = parseInt(nodeId.replace('B', ''), 10);
    var b = (window.DAG_BLOCKER_DATA || {})[id];
    if (!b) return;

    document.getElementById('dagPlaceholder').style.display = 'none';
    var content = document.getElementById('dagSidebarContent');
    content.classList.add('active');
    document.getElementById('dagSbTitle').textContent = 'Blocker #' + b.id;

    var badge = b.is_resolved
      ? '<span class="dag-blocker-badge dag-blocker-resolved">Resolved</span>'
      : '<span class="dag-blocker-badge dag-blocker-open">Open</span>';

    var m = '<div class="dag-metric"><span class="dag-metric-label">Status</span><span class="dag-metric-value">' + badge + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Type</span><span class="dag-metric-value">' + (b.blocker_type || 'external') + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Blocks Task</span><span class="dag-metric-value">#' + b.task_id + '</span></div>';
    m += '<div style="margin-top:0.75rem;font-size:0.85rem;">' + b.description + '</div>';

    document.getElementById('dagSbMetrics').innerHTML = m;
  };
})();
</script>"""


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def generate_html(task_metrics: list[dict], complexity_metrics: list[dict] = None,
                  cost_trend: list[dict] = None, all_criteria: dict[int, list[dict]] = None,
                  cost_trend_daily: list[dict] = None, cost_trend_monthly: list[dict] = None,
                  task_deps: dict[int, dict] = None, kpi_data: dict = None,
                  cost_by_domain: list[dict] = None, version: str = "",
                  dag_tasks: list[dict] = None, dag_edges: list[dict] = None,
                  dag_blockers: list[dict] = None, skill_runs: list[dict] = None,
                  tool_call_per_task: list[dict] = None,
                  tool_call_per_skill_run: list[dict] = None,
                  tool_call_per_criterion: list[dict] = None,
                  tool_call_global: list[dict] = None) -> str:
    """Generate the full HTML dashboard by composing sub-functions."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if all_criteria is None:
        all_criteria = {}
    if task_deps is None:
        task_deps = {}

    # Build per-task tool stats lookup
    tool_stats_by_task: dict[int, list[dict]] = {}
    for r in (tool_call_per_task or []):
        tid = r["task_id"]
        tool_stats_by_task.setdefault(tid, []).append(r)

    # Build per-skill-run tool stats lookup
    tool_stats_by_run: dict[int, list[dict]] = {}
    for r in (tool_call_per_skill_run or []):
        rid = r["skill_run_id"]
        tool_stats_by_run.setdefault(rid, []).append(r)

    # Build per-criterion tool stats lookup
    tool_stats_by_criterion: dict[int, list[dict]] = {}
    for r in (tool_call_per_criterion or []):
        cid = r["criterion_id"]
        tool_stats_by_criterion.setdefault(cid, []).append(r)

    # Build summary map for dependency tooltips
    summary_map: dict[int, str] = {t["id"]: t["summary"] for t in task_metrics}

    # Totals for table footer
    total_sessions = sum(t.get("session_count") or 0 for t in task_metrics)
    total_duration = sum(t.get("total_duration_seconds") or 0 for t in task_metrics)
    total_lines_added = sum(t.get("total_lines_added") or 0 for t in task_metrics)
    total_lines_removed = sum(t.get("total_lines_removed") or 0 for t in task_metrics)
    total_tokens_in = sum(t["total_tokens_in"] for t in task_metrics)
    total_tokens_out = sum(t["total_tokens_out"] for t in task_metrics)
    total_cost = sum(t["total_cost"] for t in task_metrics)
    max_cost = max((t["total_cost"] for t in task_metrics), default=0)

    # Task rows
    if task_metrics:
        task_rows = ""
        for t in task_metrics:
            tid = t['id']
            criteria_list = all_criteria.get(tid, [])
            task_rows += generate_task_row(
                t, criteria_list, task_deps, summary_map, max_cost,
                tool_stats=tool_stats_by_task.get(tid)
            )
    else:
        task_rows = '<tr><td colspan="13" class="empty">No tasks found. Run <code>tusk init</code> and add some tasks.</td></tr>'

    # Build criteria JSON for client-side rendering
    criteria_json: dict[int, dict] = {}
    for t in task_metrics:
        tid = t['id']
        cl = all_criteria.get(tid, [])
        if cl:
            pr_url = t.get("github_pr") or ""
            repo_url = ""
            if pr_url and "/pull/" in pr_url:
                repo_url = pr_url.split("/pull/")[0]
            enriched = []
            for c in cl:
                ec = dict(c)
                ec["tool_stats"] = tool_stats_by_criterion.get(c["id"], [])
                enriched.append(ec)
            criteria_json[tid] = {
                "repo_url": repo_url,
                "criteria": enriched,
            }
    _criteria_json_str = json.dumps(criteria_json).replace("</", "<\\/")
    criteria_script = f'<script>window.CRITERIA_DATA = {_criteria_json_str};</script>'

    # KPI cards
    kpi_html = generate_kpi_cards(kpi_data) if kpi_data else ""

    # Skill run costs section
    skill_runs_html = generate_skill_runs_section(skill_runs or [], tool_stats_by_run)

    # Project-wide tool cost aggregate
    global_tool_costs_html = generate_global_tool_costs_section(tool_call_global or [])

    # Charts
    charts_html = generate_charts_section(
        cost_trend or [], cost_trend_daily or [], cost_trend_monthly or [],
        cost_by_domain or []
    )

    # Complexity
    complexity_html = generate_complexity_section(complexity_metrics)

    # DAG section
    dag_html = generate_dag_section(
        dag_tasks or [], dag_edges or [], dag_blockers or []
    )

    css = generate_css()
    header = generate_header(now)
    footer = generate_footer(now, version)
    filter_bar = generate_filter_bar()
    table_header = generate_table_header()
    table_footer = generate_table_footer(total_sessions, total_duration, total_lines_added,
                                         total_lines_removed, total_tokens_in, total_tokens_out, total_cost)
    pagination = generate_pagination()
    js = generate_js()

    # Inline script to set theme before first paint (prevents flash)
    theme_init = """\
<script>
(function() {
  var saved = localStorage.getItem('tusk-theme');
  if (saved === 'dark' || saved === 'light') {
    document.documentElement.setAttribute('data-theme', saved);
  } else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
    document.documentElement.setAttribute('data-theme', 'dark');
  } else {
    document.documentElement.setAttribute('data-theme', 'light');
  }
})();
</script>"""

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tusk &mdash; Task Metrics</title>
{theme_init}
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
<script>
(function() {{
  var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  mermaid.initialize({{
    startOnLoad: false,
    securityLevel: 'loose',
    theme: isDark ? 'dark' : 'default'
  }});
}})();
</script>
<style>
{css}
</style>
</head>
<body>

{header}

<div id="tab-dashboard" class="tab-panel active">
  <div class="container">
    {kpi_html}
    {charts_html}
    <div class="panel">
      {filter_bar}
      <table id="metricsTable">
        {table_header}
        <tbody id="metricsBody">
          {task_rows}
        </tbody>
        {table_footer}
      </table>
      {pagination}
    </div>{complexity_html}
  </div>
</div>

<div id="tab-dag" class="tab-panel dag-tab-panel">
  {dag_html}
</div>

<div id="tab-skills" class="tab-panel">
  <div class="container">
    {global_tool_costs_html}
    {skill_runs_html}
  </div>
</div>

{footer}

{criteria_script}
{js}

</body>
</html>"""


def main():
    # Extract --debug before manual positional parsing
    argv = sys.argv[1:]
    debug = "--debug" in argv
    if debug:
        argv = [a for a in argv if a != "--debug"]

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="[debug] %(message)s",
        stream=sys.stderr,
    )

    if len(argv) < 2:
        print("Usage: tusk dashboard [--debug]", file=sys.stderr)
        sys.exit(1)

    db_path = argv[0]
    log.debug("DB path: %s", db_path)

    if not os.path.isfile(db_path):
        print(f"Error: Database not found at {db_path}", file=sys.stderr)
        print("Run 'tusk init' first.", file=sys.stderr)
        sys.exit(1)

    # Fetch data
    conn = get_connection(db_path)
    try:
        task_metrics = fetch_task_metrics(conn)
        kpi_data = fetch_kpi_data(conn)
        cost_by_domain = fetch_cost_by_domain(conn)
        complexity_metrics = fetch_complexity_metrics(conn)
        cost_trend = fetch_cost_trend(conn)
        cost_trend_daily = fetch_cost_trend_daily(conn)
        cost_trend_monthly = fetch_cost_trend_monthly(conn)
        all_criteria = fetch_all_criteria(conn)
        task_deps = fetch_task_dependencies(conn)
        # DAG data
        dag_tasks = fetch_dag_tasks(conn)
        dag_edges = fetch_edges(conn)
        dag_blockers = fetch_blockers(conn)
        # Skill run cost history
        skill_runs = fetch_skill_runs(conn)
        # Per-task tool call stats (for inline drilldown panels)
        tool_call_per_task = fetch_tool_call_stats_per_task(conn)
        # Per-skill-run tool call stats (for drilldown panels in skill-run table)
        tool_call_per_skill_run = fetch_tool_call_stats_per_skill_run(conn)
        # Per-criterion tool call stats (for inline drilldown inside each criterion entry)
        tool_call_per_criterion = fetch_tool_call_stats_per_criterion(conn)
        # Project-wide tool call stats (for Skills tab aggregate view)
        tool_call_global = fetch_tool_call_stats_global(conn)
    finally:
        conn.close()

    log.debug("Cost by domain: %s", cost_by_domain)

    # Read VERSION — check script dir first, then repo root (parent of DB dir)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    version = ""
    for candidate in [
        os.path.join(script_dir, "VERSION"),
        os.path.join(os.path.dirname(db_path), "..", "VERSION"),
    ]:
        if os.path.isfile(candidate):
            with open(candidate) as vf:
                version = vf.read().strip()
            break
    log.debug("Version: %s", version)

    # Generate HTML
    html_content = generate_html(
        task_metrics, complexity_metrics, cost_trend, all_criteria,
        cost_trend_daily, cost_trend_monthly, task_deps, kpi_data,
        cost_by_domain, version,
        dag_tasks, dag_edges, dag_blockers, skill_runs,
        tool_call_per_task, tool_call_per_skill_run,
        tool_call_per_criterion, tool_call_global
    )
    log.debug("Generated %d bytes of HTML", len(html_content))

    # Write to tusk/dashboard.html (same dir as DB)
    db_dir = os.path.dirname(db_path)
    output_path = os.path.join(db_dir, "dashboard.html")
    with open(output_path, "w") as f:
        f.write(html_content)
    log.debug("Wrote dashboard to %s", output_path)

    print(f"Dashboard written to {output_path}")

    # Open in browser
    webbrowser.open(f"file://{os.path.abspath(output_path)}")


if __name__ == "__main__":
    main()
