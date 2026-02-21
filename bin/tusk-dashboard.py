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
import json
import logging
import os
import sqlite3
import sys
import webbrowser
from collections import defaultdict, deque
from datetime import datetime, timedelta

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
    """Format an ISO datetime string as YYYY-MM-DD HH:MM:SS[.mmm]."""
    if dt_str is None:
        return '<span class="text-muted-dash">&mdash;</span>'
    try:
        if '.' in dt_str:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S.%f")
            return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return esc(dt_str)


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
    try:
        if '.' in dt_str:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S.%f")
        else:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ""
    seconds = int((datetime.now() - dt).total_seconds())
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
    return """\
:root {
  /* Colors */
  --bg: #f8fafc;
  --bg-panel: #ffffff;
  --bg-subtle: #f1f5f9;
  --text: #0f172a;
  --text-secondary: #475569;
  --text-muted: #94a3b8;
  --border: #e2e8f0;
  --accent: #3b82f6;
  --accent-light: #dbeafe;
  --success: #16a34a;
  --success-light: #dcfce7;
  --warning: #d97706;
  --warning-light: #fef3c7;
  --danger: #dc2626;
  --danger-light: #fef2f2;
  --info: #0ea5e9;
  --info-light: #e0f2fe;

  /* Spacing (4px base) */
  --sp-1: 4px;
  --sp-2: 8px;
  --sp-3: 12px;
  --sp-4: 16px;
  --sp-5: 20px;
  --sp-6: 24px;
  --sp-7: 28px;
  --sp-8: 32px;

  /* Typography */
  --text-xs: 0.75rem;
  --text-sm: 0.875rem;
  --text-base: 1rem;
  --text-lg: 1.125rem;
  --text-xl: 1.25rem;
  --text-2xl: 1.5rem;

  /* Radii */
  --radius-sm: 4px;
  --radius: 8px;
  --radius-lg: 12px;
  --radius-full: 9999px;

  /* Shadows */
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.05);
  --shadow: 0 1px 3px rgba(0,0,0,0.08);
  --shadow-md: 0 4px 6px rgba(0,0,0,0.1);

  /* Legacy alias */
  --hover: var(--bg-subtle);
}

html[data-theme="dark"] {
  --bg: #0f172a;
  --bg-panel: #1e293b;
  --bg-subtle: #334155;
  --text: #f1f5f9;
  --text-secondary: #cbd5e1;
  --text-muted: #64748b;
  --border: #334155;
  --accent: #60a5fa;
  --accent-light: #1e3a5f;
  --success: #4ade80;
  --success-light: #14532d;
  --warning: #fbbf24;
  --warning-light: #78350f;
  --danger: #f87171;
  --danger-light: #7f1d1d;
  --info: #38bdf8;
  --info-light: #1e3a5f;
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.2);
  --shadow: 0 1px 3px rgba(0,0,0,0.3);
  --shadow-md: 0 4px 6px rgba(0,0,0,0.4);
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
}

.header {
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border);
  padding: var(--sp-4) var(--sp-8);
  display: flex;
  justify-content: space-between;
  align-items: center;
  box-shadow: var(--shadow);
}

.header h1 {
  font-size: var(--text-2xl);
  font-weight: 700;
}

.header .timestamp {
  color: var(--text-muted);
  font-size: var(--text-sm);
}

.container {
  max-width: 1200px;
  margin: 0 auto;
  padding: var(--sp-6);
}

/* KPI Cards */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: var(--sp-4);
  margin-bottom: var(--sp-6);
}

.kpi-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--sp-5) var(--sp-4);
  box-shadow: var(--shadow-sm);
}

.kpi-label {
  font-size: var(--text-xs);
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-weight: 600;
}

.kpi-value {
  font-size: var(--text-2xl);
  font-weight: 700;
  margin-top: var(--sp-1);
  font-variant-numeric: tabular-nums;
}

.kpi-sub {
  font-size: var(--text-xs);
  color: var(--text-muted);
  margin-top: var(--sp-1);
}

/* Table */
.panel {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}

thead th {
  text-align: left;
  padding: var(--sp-3) var(--sp-4);
  border-bottom: 2px solid var(--border);
  font-weight: 600;
  color: var(--text-muted);
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  white-space: nowrap;
  cursor: pointer;
  user-select: none;
  position: relative;
}

thead th .sort-arrow {
  display: inline-block;
  margin-left: 0.3em;
  font-size: 0.65rem;
  opacity: 0.3;
}

thead th.sort-asc .sort-arrow,
thead th.sort-desc .sort-arrow {
  opacity: 1;
  color: var(--accent);
}

tbody td {
  padding: 0.6rem var(--sp-4);
  border-bottom: 1px solid var(--border);
}

tbody tr:last-child td {
  border-bottom: none;
}

tbody tr:hover {
  background: var(--bg-subtle);
}

tr.muted td {
  color: var(--text-muted);
}

tfoot td {
  padding: var(--sp-3) var(--sp-4);
  border-top: 2px solid var(--border);
  font-weight: 700;
  font-size: var(--text-sm);
}

.col-id {
  white-space: nowrap;
  color: var(--text-muted);
  font-weight: 600;
  font-size: 0.8rem;
}

.col-summary {
  max-width: 400px;
}

.col-summary .summary-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.col-domain {
  white-space: nowrap;
  font-size: 0.8rem;
}

.col-sessions {
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.col-model {
  white-space: nowrap;
  font-size: 0.75rem;
  color: var(--text-muted);
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.col-duration {
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  font-size: 0.85rem;
}

.col-lines {
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  font-size: 0.85rem;
}

.lines-added {
  color: var(--success);
}

.lines-removed {
  color: var(--danger);
}

.col-updated {
  white-space: nowrap;
  font-size: 0.8rem;
  color: var(--text-muted);
}

.cost-heat-1 { background: rgba(239, 68, 68, 0.05); }
.cost-heat-2 { background: rgba(239, 68, 68, 0.10); }
.cost-heat-3 { background: rgba(239, 68, 68, 0.15); }
.cost-heat-4 { background: rgba(239, 68, 68, 0.20); }
.cost-heat-5 { background: rgba(239, 68, 68, 0.28); }

html[data-theme="dark"] .cost-heat-1 { background: rgba(248, 113, 113, 0.06); }
html[data-theme="dark"] .cost-heat-2 { background: rgba(248, 113, 113, 0.12); }
html[data-theme="dark"] .cost-heat-3 { background: rgba(248, 113, 113, 0.18); }
html[data-theme="dark"] .cost-heat-4 { background: rgba(248, 113, 113, 0.24); }
html[data-theme="dark"] .cost-heat-5 { background: rgba(248, 113, 113, 0.32); }

/* Dependency badges */
.dep-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 0.3rem;
  margin-top: 0.2rem;
}

.dep-group {
  display: inline-flex;
  align-items: center;
  gap: 0.15rem;
}

.dep-label {
  font-size: 0.6rem;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.03em;
  margin-right: 0.1rem;
}

.dep-link {
  font-size: 0.65rem;
  font-weight: 600;
  padding: 0.05rem 0.3rem;
  border-radius: var(--radius-sm);
  text-decoration: none;
  cursor: pointer;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.dep-type-blocks {
  background: var(--danger-light);
  color: #991b1b;
}

.dep-type-contingent {
  background: #e0e7ff;
  color: #3730a3;
}

.dep-link:hover {
  text-decoration: underline;
  filter: brightness(0.9);
}

tr.dep-highlight {
  animation: dep-flash 2s ease-out;
}

@keyframes dep-flash {
  0% { background: var(--accent-light); }
  100% { background: transparent; }
}

.col-wsjf {
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  font-weight: 600;
  font-size: 0.8rem;
}

.col-tokens-in,
.col-tokens-out,
.col-cost {
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.status-badge {
  font-size: 0.7rem;
  font-weight: 600;
  padding: 0.15rem 0.5rem;
  border-radius: var(--radius-sm);
  white-space: nowrap;
}

.status-to-do {
  background: var(--accent-light);
  color: var(--accent);
}

.status-in-progress {
  background: var(--warning-light);
  color: var(--warning);
}

.status-done {
  background: var(--success-light);
  color: var(--success);
}

html[data-theme="dark"] .dep-type-blocks {
  background: #7f1d1d;
  color: #fca5a5;
}
html[data-theme="dark"] .dep-type-contingent {
  background: #312e81;
  color: #a5b4fc;
}

.empty {
  text-align: center;
  padding: var(--sp-8) var(--sp-4);
  color: var(--text-muted);
}

.empty code {
  background: var(--bg-subtle);
  padding: 0.15rem 0.4rem;
  border-radius: var(--radius-sm);
  font-size: 0.85em;
}

.section-header {
  padding: var(--sp-3) var(--sp-4);
  font-weight: 700;
  font-size: var(--text-sm);
  border-bottom: 1px solid var(--border);
}

.col-complexity {
  white-space: nowrap;
  font-weight: 600;
}

.complexity-badge {
  font-size: var(--text-xs);
  font-weight: 700;
  padding: 0.15rem 0.5rem;
  border-radius: var(--radius-sm);
  background: var(--accent-light);
  color: var(--accent);
}

.col-count,
.col-avg-sessions,
.col-avg-duration,
.col-avg-cost {
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.col-expected {
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  color: var(--text-muted);
  font-size: 0.8rem;
}

.tier-exceeds {
  background: var(--danger-light);
}

.tier-exceeds .col-avg-sessions {
  color: var(--danger);
  font-weight: 700;
}

.tier-flag {
  font-size: var(--text-xs);
}

.text-muted-dash {
  color: var(--text-muted);
}

/* Cost trend tabs */
.cost-trend-tabs {
  display: flex;
  gap: 0.25rem;
}

.cost-tab {
  font-size: var(--text-xs);
  font-weight: 600;
  padding: 0.2rem 0.6rem;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: all 0.15s;
}

.cost-tab:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.cost-tab.active {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}

/* Collapsible criteria rows */
tr.expandable {
  cursor: pointer;
}

tr.expandable:hover .expand-icon {
  color: var(--accent);
}

.expand-icon {
  display: inline-block;
  font-size: 0.6rem;
  transition: transform 0.15s;
  color: var(--text-muted);
}

tr.expandable.expanded .expand-icon {
  transform: rotate(90deg);
}

.criteria-row td {
  padding: 0 !important;
  border-bottom: 1px solid var(--border);
}

.criteria-detail {
  padding: 0.5rem var(--sp-4) 0.5rem 2.5rem;
  background: var(--bg);
}

.criterion-item {
  padding: 0.25rem 0;
  font-size: 0.8rem;
  display: flex;
  align-items: baseline;
  gap: 0.4rem;
}

.criterion-status {
  min-width: 3.5em;
  flex-shrink: 0;
  text-align: center;
}

.criterion-done {
  color: var(--success);
}

.criterion-pending {
  color: var(--text-muted);
}

.criterion-id {
  font-size: 0.7rem;
  font-weight: 600;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
  min-width: 2.5em;
  opacity: 0;
  transition: opacity 0.15s;
}

.criterion-item:hover .criterion-id {
  opacity: 1;
}

.criterion-text {
  flex: 1;
  min-width: 0;
}

.criterion-badges {
  display: flex;
  gap: 0.3rem;
  margin-left: auto;
  flex-shrink: 0;
}

.criterion-source {
  font-size: 0.65rem;
  font-weight: 600;
  padding: 0.1rem 0.35rem;
  border-radius: var(--radius-sm);
  background: var(--bg-subtle);
  color: var(--text-muted);
}

.criterion-cost {
  font-size: 0.65rem;
  font-weight: 600;
  padding: 0.1rem 0.35rem;
  border-radius: var(--radius-sm);
  background: var(--success-light);
  color: #166534;
  font-variant-numeric: tabular-nums;
}

.criterion-time {
  font-size: 0.65rem;
  font-weight: 600;
  padding: 0.1rem 0.35rem;
  border-radius: var(--radius-sm);
  background: var(--accent-light);
  color: #1e40af;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.criterion-commit {
  font-size: 0.65rem;
  font-weight: 600;
  padding: 0.1rem 0.35rem;
  border-radius: var(--radius-sm);
  background: var(--warning-light);
  color: #92400e;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  font-variant-numeric: tabular-nums;
  text-decoration: none;
  white-space: nowrap;
}

a.criterion-commit:hover {
  background: #fde68a;
  text-decoration: underline;
}

html[data-theme="dark"] .criterion-commit {
  background: var(--warning-light);
  color: var(--warning);
}
html[data-theme="dark"] a.criterion-commit:hover {
  background: #92400e;
}
html[data-theme="dark"] .criterion-cost {
  background: var(--success-light);
  color: #86efac;
}
html[data-theme="dark"] .criterion-time {
  background: var(--accent-light);
  color: #93c5fd;
}

.criterion-type {
  font-size: 0.65rem;
  font-weight: 600;
  padding: 0.1rem 0.35rem;
  border-radius: var(--radius-sm);
  background: #f3e8ff;
  color: #7c3aed;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.criterion-type-code {
  background: var(--warning-light);
  color: var(--warning);
}

.criterion-type-test {
  background: var(--success-light);
  color: var(--success);
}

.criterion-type-file {
  background: var(--accent-light);
  color: #1e40af;
}

html[data-theme="dark"] .criterion-type {
  background: #4c1d95;
  color: #c4b5fd;
}
html[data-theme="dark"] .criterion-type-code {
  background: var(--warning-light);
  color: var(--warning);
}
html[data-theme="dark"] .criterion-type-test {
  background: var(--success-light);
  color: var(--success);
}
html[data-theme="dark"] .criterion-type-file {
  background: var(--accent-light);
  color: #93c5fd;
}

/* Criteria sort bar */
.criteria-sort-bar {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.3rem 0;
  margin-bottom: 0.3rem;
  border-bottom: 1px solid var(--border);
}

.criteria-sort-label {
  font-size: 0.7rem;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-right: 0.2rem;
}

.criteria-sort-btn {
  font-size: 0.7rem;
  font-weight: 600;
  padding: 0.15rem 0.45rem;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  user-select: none;
  transition: all 0.15s;
  white-space: nowrap;
}

.criteria-sort-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.criteria-sort-btn .sort-arrow {
  display: inline-block;
  margin-left: 0.2em;
  font-size: 0.55rem;
  opacity: 0.3;
}

.criteria-sort-btn.sort-asc .sort-arrow,
.criteria-sort-btn.sort-desc .sort-arrow {
  opacity: 1;
  color: var(--accent);
}

.criterion-empty {
  font-size: 0.8rem;
  color: var(--text-muted);
  font-style: italic;
}

/* Criteria view mode buttons */
.criteria-view-modes {
  display: flex;
  gap: 0;
}

.criteria-view-btn {
  font-size: 0.7rem;
  font-weight: 600;
  padding: 0.15rem 0.45rem;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  user-select: none;
  transition: all 0.15s;
  white-space: nowrap;
}

.criteria-view-btn:first-child {
  border-radius: var(--radius-sm) 0 0 var(--radius-sm);
}

.criteria-view-btn:last-child {
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}

.criteria-view-btn:not(:first-child) {
  border-left: none;
}

.criteria-view-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.criteria-view-btn.active {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}

.criteria-sort-sep {
  width: 1px;
  height: 1em;
  background: var(--border);
  margin: 0 0.2rem;
}

/* Criteria type groups */
.criteria-type-group {
  margin-bottom: 0.25rem;
}

.criteria-type-group:last-child {
  margin-bottom: 0;
}

.criteria-group-header {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  padding: 0.3rem 0.2rem;
  font-size: var(--text-xs);
  font-weight: 700;
  color: var(--text);
  cursor: pointer;
  user-select: none;
  border-radius: var(--radius-sm);
  transition: background 0.1s;
}

.criteria-group-header:hover {
  background: var(--bg-subtle);
}

.criteria-group-icon {
  display: inline-block;
  font-size: 0.55rem;
  transition: transform 0.15s;
  color: var(--text-muted);
  transform: rotate(90deg);
}

.criteria-type-group.collapsed .criteria-group-icon {
  transform: rotate(0deg);
}

.criteria-group-name {
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.criteria-group-commit-link {
  font-family: 'SF Mono', SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.7rem;
  color: var(--accent);
  text-decoration: none;
}

.criteria-group-commit-link:hover {
  text-decoration: underline;
}

.criteria-group-commit-hash {
  font-family: 'SF Mono', SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.7rem;
}

.criteria-group-time {
  font-weight: 400;
  color: var(--text-muted);
  font-size: 0.7rem;
}

.criteria-group-count {
  font-weight: 400;
  color: var(--text-muted);
}

.criteria-group-all-done .criteria-group-count {
  color: var(--success);
}

.criteria-group-progress {
  height: 3px;
  background: var(--border);
  border-radius: 2px;
  margin: 0.15rem 0.2rem 0;
  overflow: hidden;
}

.criteria-group-progress-fill {
  height: 100%;
  background: var(--accent);
  border-radius: 2px;
  transition: width 0.2s;
}

.criteria-group-all-done .criteria-group-progress-fill {
  background: var(--success);
}

.criteria-group-cost {
  font-size: 0.65rem;
  font-weight: 600;
  padding: 0.1rem 0.35rem;
  border-radius: var(--radius-sm);
  background: var(--success-light);
  color: #166534;
  font-variant-numeric: tabular-nums;
  margin-left: 0.4rem;
}

.criteria-group-tokens {
  font-size: 0.65rem;
  font-weight: 600;
  padding: 0.1rem 0.35rem;
  border-radius: var(--radius-sm);
  background: var(--accent-light);
  color: #1e40af;
  font-variant-numeric: tabular-nums;
  margin-left: 0.3rem;
}

html[data-theme="dark"] .criteria-group-cost {
  background: var(--success-light);
  color: #86efac;
}
html[data-theme="dark"] .criteria-group-tokens {
  background: var(--accent-light);
  color: #93c5fd;
}

.criteria-group-items {
  padding-left: 0.5rem;
}

.criteria-type-group.collapsed .criteria-group-items {
  display: none;
}

/* Filter bar */
.filter-bar {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4);
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}

.filter-chips {
  display: flex;
  gap: 0.35rem;
}

.filter-chip {
  font-size: var(--text-xs);
  font-weight: 600;
  padding: 0.25rem 0.65rem;
  border-radius: var(--radius-full);
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: all 0.15s;
}

.filter-chip:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.filter-chip.active {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}

.search-input {
  flex: 1;
  min-width: 160px;
  max-width: 300px;
  padding: 0.35rem 0.65rem;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
  font-size: 0.8rem;
  outline: none;
}

.search-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-light);
}

.search-input::placeholder {
  color: var(--text-muted);
}

/* Filter dropdowns */
.filter-select {
  padding: 0.3rem 0.5rem;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
  font-size: 0.8rem;
  outline: none;
  cursor: pointer;
  min-width: 100px;
}

.filter-select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-light);
}

/* Active filter badge + clear */
.filter-meta {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  margin-left: auto;
}

.filter-badge {
  font-size: 0.7rem;
  font-weight: 700;
  padding: 0.1rem 0.45rem;
  border-radius: var(--radius-full);
  background: var(--accent);
  color: #fff;
  min-width: 1.3em;
  text-align: center;
  font-variant-numeric: tabular-nums;
}

.filter-badge.hidden {
  display: none;
}

.clear-filters {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--accent);
  background: none;
  border: none;
  cursor: pointer;
  padding: 0.15rem 0.3rem;
  border-radius: var(--radius-sm);
}

.clear-filters:hover {
  background: var(--accent-light);
}

.clear-filters.hidden {
  display: none;
}

/* Pagination */
.pagination-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.6rem var(--sp-4);
  border-top: 1px solid var(--border);
  font-size: 0.8rem;
  color: var(--text-muted);
}

.pagination-bar .page-info {
  font-variant-numeric: tabular-nums;
}

.pagination-controls {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.page-size-select {
  padding: 0.2rem 0.4rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text);
  font-size: 0.8rem;
  cursor: pointer;
}

.page-btn {
  padding: 0.25rem 0.6rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text);
  font-size: 0.8rem;
  cursor: pointer;
}

.page-btn:hover:not(:disabled) {
  background: var(--bg-subtle);
  border-color: var(--accent);
}

.page-btn:disabled {
  opacity: 0.35;
  cursor: default;
}

/* Theme toggle */
.theme-toggle {
  background: none;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 0.35rem 0.5rem;
  cursor: pointer;
  color: var(--text-muted);
  font-size: 1.1rem;
  line-height: 1;
  transition: color 0.2s, border-color 0.2s, background 0.2s;
  display: flex;
  align-items: center;
  gap: 0.3rem;
}
.theme-toggle:hover {
  color: var(--accent);
  border-color: var(--accent);
  background: var(--bg-subtle);
}
.theme-toggle .icon-sun,
.theme-toggle .icon-moon { display: none; }
html[data-theme="dark"] .theme-toggle .icon-sun { display: inline; }
html[data-theme="light"] .theme-toggle .icon-moon { display: inline; }
html:not([data-theme]) .theme-toggle .icon-moon { display: inline; }

/* Footer */
.footer {
  text-align: center;
  padding: var(--sp-4) var(--sp-6);
  margin-top: var(--sp-6);
  font-size: var(--text-xs);
  color: var(--text-muted);
  border-top: 1px solid var(--border);
}
.footer span + span::before {
  content: " \\00b7 ";
  margin: 0 0.3em;
}

/* Transitions */
.kpi-card {
  transition: box-shadow 0.2s, border-color 0.2s;
}
.kpi-card:hover {
  box-shadow: var(--shadow-md);
  border-color: var(--accent);
}
.filter-chip, .cost-tab, .page-btn, .criteria-sort-btn {
  transition: all 0.15s;
}
tbody tr {
  transition: background 0.1s;
}

/* Responsive: tablet */
@media (max-width: 900px) {
  .kpi-grid {
    grid-template-columns: repeat(3, 1fr);
  }
  .col-updated, .col-wsjf, .col-model {
    display: none;
  }
  .header {
    padding: var(--sp-3) var(--sp-4);
  }
  .header h1 {
    font-size: var(--text-lg);
  }
  .container {
    padding: var(--sp-4);
  }
}

/* Responsive: mobile */
@media (max-width: 600px) {
  .kpi-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  .col-updated, .col-wsjf, .col-domain, .col-tokens-in, .col-tokens-out {
    display: none;
  }
  .col-summary {
    max-width: 180px;
  }
  .header {
    flex-wrap: wrap;
    gap: var(--sp-2);
  }
  .header h1 {
    font-size: var(--text-base);
  }
  .filter-bar {
    gap: var(--sp-2);
  }
  .search-input {
    min-width: 120px;
    max-width: none;
    flex: 1 1 100%;
    order: 10;
  }
  .pagination-bar {
    flex-wrap: wrap;
    gap: var(--sp-2);
    justify-content: center;
  }
  .kpi-value {
    font-size: var(--text-xl);
  }
  .container {
    padding: var(--sp-3);
  }
}

/* ---------------------------------------------------------------------------
   Tab navigation
   --------------------------------------------------------------------------- */

.tab-bar {
  display: flex;
  gap: 0;
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border);
  padding: 0 var(--sp-6);
}

.tab-btn {
  padding: var(--sp-3) var(--sp-5);
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text-muted);
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
  white-space: nowrap;
}

.tab-btn:hover {
  color: var(--text);
}

.tab-btn.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}

.tab-panel {
  display: none;
}

.tab-panel.active {
  display: block;
}

/* DAG tab uses flex layout to fill remaining viewport height */
.tab-panel.dag-tab-panel.active {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 110px);
  overflow: hidden;
}

/* ---------------------------------------------------------------------------
   DAG panel styles
   --------------------------------------------------------------------------- */

.dag-toolbar {
  display: flex;
  align-items: center;
  gap: var(--sp-4);
  padding: var(--sp-3) var(--sp-6);
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

.dag-toggle-label {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--text-sm);
  color: var(--text-secondary);
  cursor: pointer;
  user-select: none;
}

.dag-toggle-label input[type="checkbox"] {
  accent-color: var(--accent);
}

.dag-main {
  display: flex;
  flex: 1;
  overflow: hidden;
}

.dag-graph-panel {
  flex: 1;
  overflow: auto;
  padding: var(--sp-4);
  display: flex;
  flex-direction: column;
}

.dag-graph-panel .mermaid {
  flex: 1;
}

.dag-sidebar {
  width: 320px;
  background: var(--bg-panel);
  border-left: 1px solid var(--border);
  box-shadow: var(--shadow);
  overflow-y: auto;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
}

.dag-sidebar-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 1;
  color: var(--text-muted);
  font-size: var(--text-sm);
  padding: var(--sp-6);
  text-align: center;
}

.dag-sidebar-content {
  display: none;
  padding: var(--sp-4);
}

.dag-sidebar-content.active {
  display: block;
}

.dag-sidebar-content h2 {
  font-size: var(--text-lg);
  font-weight: 700;
  margin-bottom: var(--sp-4);
  word-break: break-word;
}

.dag-metric {
  display: flex;
  justify-content: space-between;
  padding: var(--sp-1) 0;
  border-bottom: 1px solid var(--border);
  font-size: var(--text-sm);
}

.dag-metric:last-child {
  border-bottom: none;
}

.dag-metric-label {
  color: var(--text-muted);
  font-weight: 500;
}

.dag-metric-value {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.dag-legend {
  padding: var(--sp-3) var(--sp-4);
  border-top: 1px solid var(--border);
  font-size: var(--text-xs);
  color: var(--text-muted);
  flex-shrink: 0;
}

.dag-legend-title {
  font-weight: 600;
  margin-bottom: var(--sp-1);
}

.dag-legend-row {
  display: flex;
  gap: var(--sp-3);
  flex-wrap: wrap;
  margin-bottom: 2px;
}

.dag-legend-item {
  display: flex;
  align-items: center;
  gap: 4px;
}

.dag-legend-swatch {
  width: 12px;
  height: 12px;
  border-radius: 3px;
  flex-shrink: 0;
}

.dag-hint {
  text-align: center;
  color: var(--text-muted);
  font-size: var(--text-sm);
  padding: var(--sp-2);
}

.dag-hint code {
  background: var(--bg-subtle);
  padding: 2px 6px;
  border-radius: 3px;
  font-size: 0.85em;
}

.dag-blocker-badge {
  font-size: var(--text-xs);
  font-weight: 600;
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  white-space: nowrap;
}

.dag-blocker-open {
  background: var(--danger-light);
  color: var(--danger);
}

.dag-blocker-resolved {
  background: var(--bg-subtle);
  color: var(--text-muted);
}

.dag-blocker-item {
  padding: var(--sp-1) 0;
  border-bottom: 1px solid var(--border);
  font-size: var(--text-sm);
}

.dag-blocker-item:last-child {
  border-bottom: none;
}

.dag-blocker-header {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  margin-bottom: 2px;
}

.dag-blocker-type {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.dag-blocker-desc {
  font-size: var(--text-xs);
  color: var(--text);
  word-break: break-word;
}

@media (max-width: 768px) {
  .dag-sidebar {
    display: none;
  }
  .tab-bar {
    padding: 0 var(--sp-3);
  }
}"""


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
  <button class="tab-btn active" data-tab="dashboard">Dashboard</button>
  <button class="tab-btn" data-tab="dag">DAG</button>
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
    })

    domain_data = json.dumps(cost_by_domain or [])

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


def generate_task_row(t: dict, criteria_list: list[dict], task_deps: dict, summary_map: dict, max_cost: float = 0) -> str:
    """Generate a single task table row (and optional criteria detail row)."""
    has_data = t["session_count"] > 0
    status_val = esc(t['status'])
    tid = t['id']
    has_criteria = len(criteria_list) > 0
    toggle_icon = '<span class="expand-icon">&#9654;</span> ' if has_criteria else ''

    row_classes = []
    if not has_data:
        row_classes.append('muted')
    if has_criteria:
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

    if has_criteria:
        row += generate_criteria_detail(tid)

    return row


def generate_criteria_detail(tid: int) -> str:
    """Generate the collapsible criteria detail row shell (rendered client-side from JSON)."""
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

    return (
        f'<tr class="criteria-row" data-parent="{tid}" style="display:none">\n'
        f'  <td colspan="14"><div class="criteria-detail" data-tid="{tid}">'
        f'{sort_bar}'
        f'<div class="criteria-render-target"></div>'
        f'</div></td>\n'
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

    return '<div class="criterion-item ' + css + '" data-sort-completed="' + escHtml(cr.completed_at || '') + '" '
      + 'data-sort-cost="' + (cr.cost_dollars || 0) + '" data-sort-commit="' + escHtml(cr.commit_hash || '') + '" data-cid="' + cr.id + '">'
      + '<span class="criterion-id">#' + cr.id + '</span>'
      + '<span class="criterion-status">' + check + '</span>'
      + '<span class="criterion-text">' + escHtml(cr.criterion) + '</span>'
      + badges + '</div>';
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
                  dag_blockers: list[dict] = None) -> str:
    """Generate the full HTML dashboard by composing sub-functions."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if all_criteria is None:
        all_criteria = {}
    if task_deps is None:
        task_deps = {}

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
            task_rows += generate_task_row(t, criteria_list, task_deps, summary_map, max_cost)
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
            criteria_json[tid] = {
                "repo_url": repo_url,
                "criteria": cl,
            }
    criteria_script = f'<script>window.CRITERIA_DATA = {json.dumps(criteria_json)};</script>'

    # KPI cards
    kpi_html = generate_kpi_cards(kpi_data) if kpi_data else ""

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
        dag_tasks, dag_edges, dag_blockers
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
