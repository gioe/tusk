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
                  tm.updated_at
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
               COALESCE(SUM(s.tokens_out), 0) as total_tokens_out,
               COALESCE(SUM(s.lines_added), 0) as total_lines_added,
               COALESCE(SUM(s.lines_removed), 0) as total_lines_removed
           FROM task_sessions s"""
    ).fetchone()

    tasks_completed = conn.execute(
        "SELECT COUNT(*) as count FROM tasks WHERE status = 'Done'"
    ).fetchone()["count"]

    tasks_total = conn.execute(
        "SELECT COUNT(*) as count FROM tasks"
    ).fetchone()["count"]

    criteria_row = conn.execute(
        """SELECT
               COUNT(*) as total,
               SUM(CASE WHEN is_completed = 1 THEN 1 ELSE 0 END) as completed
           FROM acceptance_criteria"""
    ).fetchone()

    result = {
        "total_cost": row["total_cost"],
        "total_tokens_in": row["total_tokens_in"],
        "total_tokens_out": row["total_tokens_out"],
        "total_tokens": row["total_tokens_in"] + row["total_tokens_out"],
        "total_lines_added": row["total_lines_added"],
        "total_lines_removed": row["total_lines_removed"],
        "tasks_completed": tasks_completed,
        "tasks_total": tasks_total,
        "avg_cost_per_task": row["total_cost"] / tasks_completed if tasks_completed > 0 else 0,
        "criteria_completed": criteria_row["completed"] or 0,
        "criteria_total": criteria_row["total"] or 0,
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


def format_lines_changed(added, removed) -> str:
    """Format lines changed as +N/-M."""
    added = added or 0
    removed = removed or 0
    if added == 0 and removed == 0:
        return "0"
    return f"+{int(added)}/\u2212{int(removed)}"


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

@media (prefers-color-scheme: dark) {
  :root {
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

@media (prefers-color-scheme: dark) {
  .cost-heat-1 { background: rgba(248, 113, 113, 0.06); }
  .cost-heat-2 { background: rgba(248, 113, 113, 0.12); }
  .cost-heat-3 { background: rgba(248, 113, 113, 0.18); }
  .cost-heat-4 { background: rgba(248, 113, 113, 0.24); }
  .cost-heat-5 { background: rgba(248, 113, 113, 0.32); }
}

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

@media (prefers-color-scheme: dark) {
  .dep-type-blocks {
    background: #7f1d1d;
    color: #fca5a5;
  }
  .dep-type-contingent {
    background: #312e81;
    color: #a5b4fc;
  }
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

.criteria-header {
  padding: 0.25rem 0;
  font-size: 0.7rem;
  font-weight: 700;
  display: flex;
  align-items: baseline;
  gap: 0.4rem;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border-bottom: 1px solid var(--border);
  margin-bottom: 0.25rem;
}

.criteria-header-status {
  min-width: 3.5em;
  flex-shrink: 0;
  white-space: nowrap;
  text-align: center;
}

.criterion-status {
  min-width: 3.5em;
  flex-shrink: 0;
  text-align: center;
}

.criteria-header-label {
  font-size: 0.65rem;
  font-weight: 700;
  padding: 0.1rem 0.35rem;
  color: var(--text-muted);
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

@media (prefers-color-scheme: dark) {
  .criterion-commit {
    background: var(--warning-light);
    color: var(--warning);
  }
  a.criterion-commit:hover {
    background: #92400e;
  }
  .criterion-cost {
    background: var(--success-light);
    color: #86efac;
  }
  .criterion-time {
    background: var(--accent-light);
    color: #93c5fd;
  }
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

@media (prefers-color-scheme: dark) {
  .criterion-type {
    background: #4c1d95;
    color: #c4b5fd;
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
    color: #93c5fd;
  }
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

/* Criteria view toggle */
.criteria-view-toggle {
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

.criteria-view-toggle:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.criteria-view-toggle.active {
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

@media (prefers-color-scheme: dark) {
  .criteria-group-cost {
    background: var(--success-light);
    color: #86efac;
  }
  .criteria-group-tokens {
    background: var(--accent-light);
    color: #93c5fd;
  }
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

@media (max-width: 700px) {
  .col-summary {
    max-width: 180px;
  }
}"""


def generate_header(now: str) -> str:
    """Generate the page header bar."""
    return f"""\
<div class="header">
  <h1>Tusk &mdash; Task Metrics</h1>
  <span class="timestamp">Generated {esc(now)}</span>
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
    lines = format_lines_changed(kpi_data["total_lines_added"], kpi_data["total_lines_removed"])
    criteria_done = kpi_data["criteria_completed"]
    criteria_total = kpi_data["criteria_total"]
    criteria_pct = round(criteria_done / criteria_total * 100) if criteria_total > 0 else 0

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
  <div class="kpi-card">
    <div class="kpi-label">Lines Changed</div>
    <div class="kpi-value">{lines}</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Criteria</div>
    <div class="kpi-value">{criteria_done}/{criteria_total}</div>
    <div class="kpi-sub">{criteria_pct}% complete</div>
  </div>
</div>"""


def generate_cost_trend_svg(cost_trend: list[dict], period_key: str = "week_start", cost_key: str = "weekly_cost", period_label: str = "Weekly", svg_id: str = "") -> str:
    """Generate an inline SVG bar chart with cumulative cost line."""
    if not cost_trend:
        return '<p class="empty">No session cost data available yet.</p>'

    chart_w = 800
    chart_h = 260
    pad_left = 70
    pad_right = 20
    pad_top = 20
    pad_bottom = 60

    plot_w = chart_w - pad_left - pad_right
    plot_h = chart_h - pad_top - pad_bottom

    weeks = [row[period_key] for row in cost_trend]
    costs = [row[cost_key] for row in cost_trend]
    n = len(weeks)

    cumulative = []
    running = 0.0
    for c in costs:
        running += c
        cumulative.append(running)

    max_cost = max(costs) if costs else 1
    max_cumulative = cumulative[-1] if cumulative else 1
    if max_cost == 0:
        max_cost = 1
    if max_cumulative == 0:
        max_cumulative = 1

    bar_width = max(4, min(40, (plot_w / n) * 0.6))
    bar_gap = plot_w / n

    def format_tooltip_period(period_str: str) -> str:
        if period_label == "Daily":
            try:
                dt = datetime.strptime(period_str, "%Y-%m-%d")
                return dt.strftime("%b %d, %Y")
            except ValueError:
                return period_str
        elif period_label == "Monthly":
            try:
                dt = datetime.strptime(period_str + "-01", "%Y-%m-%d")
                return dt.strftime("%b %Y")
            except ValueError:
                return period_str
        else:
            return f"Week of {period_str}"

    bars = []
    for i, cost in enumerate(costs):
        x = pad_left + i * bar_gap + (bar_gap - bar_width) / 2
        bar_h = (cost / max_cost) * plot_h
        y = pad_top + plot_h - bar_h
        tooltip = f"{format_tooltip_period(weeks[i])}: ${cost:,.2f}"
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" '
            f'height="{bar_h:.1f}" fill="var(--accent)" opacity="0.7" rx="2">'
            f"<title>{esc(tooltip)}</title></rect>"
        )

    line_points = []
    for i, cum in enumerate(cumulative):
        x = pad_left + i * bar_gap + bar_gap / 2
        y = pad_top + plot_h - (cum / max_cumulative) * plot_h
        line_points.append(f"{x:.1f},{y:.1f}")
    polyline = (
        f'<polyline points="{" ".join(line_points)}" '
        f'fill="none" stroke="#f59e0b" stroke-width="2.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
    )

    dots = []
    for i, cum in enumerate(cumulative):
        x = pad_left + i * bar_gap + bar_gap / 2
        y = pad_top + plot_h - (cum / max_cumulative) * plot_h
        tooltip = f"Cumulative: ${cum:,.2f}"
        dots.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" '
            f'fill="#f59e0b" stroke="var(--bg-panel)" stroke-width="1.5">'
            f"<title>{esc(tooltip)}</title></circle>"
        )

    y_labels = []
    for frac in [0, 0.25, 0.5, 0.75, 1.0]:
        val = max_cost * frac
        y = pad_top + plot_h - frac * plot_h
        y_labels.append(
            f'<text x="{pad_left - 8}" y="{y:.1f}" '
            f'text-anchor="end" dominant-baseline="middle" '
            f'fill="var(--text-muted)" font-size="11">${val:,.0f}</text>'
        )
        y_labels.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" '
            f'x2="{chart_w - pad_right}" y2="{y:.1f}" '
            f'stroke="var(--border)" stroke-dasharray="3,3"/>'
        )

    cum_labels = []
    for frac in [0, 0.5, 1.0]:
        val = max_cumulative * frac
        y = pad_top + plot_h - frac * plot_h
        cum_labels.append(
            f'<text x="{chart_w - pad_right + 8}" y="{y:.1f}" '
            f'text-anchor="start" dominant-baseline="middle" '
            f'fill="#f59e0b" font-size="11">${val:,.0f}</text>'
        )

    x_labels = []
    step = max(1, n // 10)
    for i in range(0, n, step):
        x = pad_left + i * bar_gap + bar_gap / 2
        try:
            if period_label == "Monthly":
                dt = datetime.strptime(weeks[i] + "-01", "%Y-%m-%d")
                label = dt.strftime("%b %Y")
            else:
                dt = datetime.strptime(weeks[i], "%Y-%m-%d")
                label = dt.strftime("%b %d")
        except ValueError:
            label = weeks[i]
        x_labels.append(
            f'<text x="{x:.1f}" y="{pad_top + plot_h + 20}" '
            f'text-anchor="middle" fill="var(--text-muted)" '
            f'font-size="11">{esc(label)}</text>'
        )

    id_attr = f' id="{svg_id}"' if svg_id else ''
    svg = f"""<svg{id_attr} viewBox="0 0 {chart_w + 50} {chart_h}" xmlns="http://www.w3.org/2000/svg"
     style="width:100%;max-width:{chart_w + 50}px;height:auto;font-family:inherit;">
  {''.join(y_labels)}
  {''.join(cum_labels)}
  {''.join(bars)}
  {polyline}
  {''.join(dots)}
  {''.join(x_labels)}
  <!-- Legend -->
  <rect x="{pad_left}" y="{chart_h - 12}" width="12" height="12" fill="var(--accent)" opacity="0.7" rx="2"/>
  <text x="{pad_left + 16}" y="{chart_h - 1}" fill="var(--text-muted)" font-size="11">{esc(period_label)} cost</text>
  <line x1="{pad_left + 110}" y1="{chart_h - 6}" x2="{pad_left + 130}" y2="{chart_h - 6}" stroke="#f59e0b" stroke-width="2.5"/>
  <circle cx="{pad_left + 120}" cy="{chart_h - 6}" r="3" fill="#f59e0b"/>
  <text x="{pad_left + 135}" y="{chart_h - 1}" fill="#f59e0b" font-size="11">Cumulative</text>
</svg>"""
    return svg


def generate_charts_section(cost_trend: list[dict], cost_trend_daily: list[dict], cost_trend_monthly: list[dict]) -> str:
    """Generate the cost trend panel with daily/weekly/monthly tabs."""
    daily_svg = generate_cost_trend_svg(
        cost_trend_daily, period_key="day", cost_key="daily_cost", period_label="Daily"
    )
    weekly_svg = generate_cost_trend_svg(cost_trend)
    monthly_svg = generate_cost_trend_svg(
        cost_trend_monthly, period_key="month", cost_key="monthly_cost", period_label="Monthly"
    )

    return f"""\
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
    <div class="cost-trend-view" data-view="daily" style="display:none">
      {daily_svg}
    </div>
    <div class="cost-trend-view" data-view="weekly">
      {weekly_svg}
    </div>
    <div class="cost-trend-view" data-view="monthly" style="display:none">
      {monthly_svg}
    </div>
  </div>
</div>"""


def generate_filter_bar() -> str:
    """Generate the filter chips and search input."""
    return """\
<div class="filter-bar">
  <div class="filter-chips" id="statusFilters">
    <button class="filter-chip active" data-filter="All">All</button>
    <button class="filter-chip" data-filter="To Do">To Do</button>
    <button class="filter-chip" data-filter="In Progress">In Progress</button>
    <button class="filter-chip" data-filter="Done">Done</button>
  </div>
  <input type="text" class="search-input" id="searchInput" placeholder="Search tasks\u2026">
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
    <th data-col="7" data-type="num" style="text-align:right">Duration <span class="sort-arrow">\u25B2</span></th>
    <th data-col="8" data-type="num" style="text-align:right">Lines <span class="sort-arrow">\u25B2</span></th>
    <th data-col="9" data-type="num" style="text-align:right">Tokens In <span class="sort-arrow">\u25B2</span></th>
    <th data-col="10" data-type="num" style="text-align:right">Tokens Out <span class="sort-arrow">\u25B2</span></th>
    <th data-col="11" data-type="num" style="text-align:right">Cost <span class="sort-arrow">\u25B2</span></th>
    <th data-col="12" data-type="str" class="sort-desc">Updated <span class="sort-arrow">\u25BC</span></th>
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
  <td class="col-duration" data-sort="{duration_seconds}">{format_duration(duration_seconds) if duration_seconds else '<span class="text-muted-dash">&mdash;</span>'}</td>
  <td class="col-lines" data-sort="{total_lines}">{format_lines_html(lines_added, lines_removed)}</td>
  <td class="col-tokens-in" data-sort="{t['total_tokens_in']}">{format_tokens_compact(t['total_tokens_in'])}</td>
  <td class="col-tokens-out" data-sort="{t['total_tokens_out']}">{format_tokens_compact(t['total_tokens_out'])}</td>
  <td class="{cost_cls}" data-sort="{t['total_cost']}">{format_cost(t['total_cost'])}</td>
  <td class="col-updated" data-sort="{esc(t.get('updated_at') or '')}">{format_relative_time(t.get('updated_at'))}</td>
</tr>\n"""

    if has_criteria:
        row += generate_criteria_detail(tid, criteria_list, t.get("github_pr"))

    return row


def generate_criteria_detail(tid: int, criteria_list: list[dict], github_pr: str | None) -> str:
    """Generate the collapsible criteria detail row for a task."""
    pr_url = github_pr or ""
    repo_url = ""
    if pr_url and "/pull/" in pr_url:
        repo_url = pr_url.split("/pull/")[0]

    # Build individual criterion item HTML strings
    criterion_item_htmls = []
    for cr in criteria_list:
        done = cr['is_completed']
        check = '&#10003;' if done else '&#9711;'
        css = 'criterion-done' if done else 'criterion-pending'
        ctype = cr.get("criterion_type") or "manual"
        source_badge = f' <span class="criterion-source">{esc(cr["source"])}</span>' if cr.get("source") else ''
        cost_badge = f' <span class="criterion-cost">${cr["cost_dollars"]:.4f}</span>' if cr.get("cost_dollars") else ''
        time_badge = f' <span class="criterion-time">{format_date(cr["completed_at"])}</span>' if cr.get("completed_at") else ''
        type_badge = f' <span class="criterion-type criterion-type-{esc(ctype)}">{esc(ctype)}</span>'
        commit_badge = ''
        if cr.get("commit_hash"):
            if repo_url:
                commit_url = f"{repo_url}/commit/{esc(cr['commit_hash'])}"
                commit_badge = f' <a href="{commit_url}" class="criterion-commit" target="_blank">{esc(cr["commit_hash"])}</a>'
            else:
                commit_badge = f' <span class="criterion-commit">{esc(cr["commit_hash"])}</span>'
        badges = f'<span class="criterion-badges">{type_badge}{source_badge}{cost_badge}{commit_badge}{time_badge}</span>'
        sort_completed = esc(cr.get("completed_at") or "")
        sort_cost = cr.get("cost_dollars") or 0
        sort_commit = esc(cr.get("commit_hash") or "")
        item_html = (
            f'<div class="criterion-item {css}" data-sort-completed="{sort_completed}" '
            f'data-sort-cost="{sort_cost}" data-sort-commit="{sort_commit}">'
            f'<span class="criterion-id">#{cr["id"]}</span>'
            f'<span class="criterion-status">{check}</span>'
            f'<span class="criterion-text">{esc(cr["criterion"])}</span>'
            f'{badges}</div>\n'
        )
        criterion_item_htmls.append((item_html, cr))

    # Flat view: all items in original order
    flat_items = ''.join(h for h, _ in criterion_item_htmls)

    # Grouped view: group by commit_hash with collapsible headers
    commit_groups_html: dict[str | None, list[str]] = {}
    commit_counts: dict[str | None, dict[str, int]] = {}
    commit_timestamps: dict[str | None, str] = {}
    commit_costs: dict[str | None, float] = {}
    commit_tokens: dict[str | None, int] = {}
    for item_html, cr in criterion_item_htmls:
        chash = cr.get("commit_hash") or None
        commit_groups_html.setdefault(chash, []).append(item_html)
        if chash not in commit_counts:
            commit_counts[chash] = {"done": 0, "total": 0}
            commit_costs[chash] = 0.0
            commit_tokens[chash] = 0
        commit_counts[chash]["total"] += 1
        if cr["is_completed"]:
            commit_counts[chash]["done"] += 1
        commit_costs[chash] += cr.get("cost_dollars") or 0
        commit_tokens[chash] += (cr.get("tokens_in") or 0) + (cr.get("tokens_out") or 0)
        if chash and cr.get("committed_at") and chash not in commit_timestamps:
            commit_timestamps[chash] = cr["committed_at"]

    # Order: committed groups by committed_at descending, then Uncommitted last
    committed_hashes = [h for h in commit_groups_html if h is not None]
    committed_hashes.sort(key=lambda h: commit_timestamps.get(h, ""), reverse=True)
    group_order: list[str | None] = committed_hashes
    if None in commit_groups_html:
        group_order.append(None)

    grouped_items = ""
    for chash in group_order:
        counts = commit_counts[chash]
        group_cost = commit_costs[chash]
        group_tokens = commit_tokens[chash]
        cost_badge = f' <span class="criteria-group-cost">${group_cost:.4f}</span>' if group_cost else ''
        token_badge = f' <span class="criteria-group-tokens">{group_tokens:,} tok</span>' if group_tokens else ''
        all_done_cls = " criteria-group-all-done" if counts["done"] == counts["total"] else ""
        group_key = esc(chash) if chash else "uncommitted"
        grouped_items += f'<div class="criteria-type-group{all_done_cls}" data-group-type="{group_key}">'
        if chash:
            short_hash = esc(chash[:8])
            if repo_url:
                commit_link = f'<a href="{repo_url}/commit/{esc(chash)}" class="criteria-group-commit-link" target="_blank">{short_hash}</a>'
            else:
                commit_link = f'<span class="criteria-group-commit-hash">{short_hash}</span>'
            ts = format_date(commit_timestamps.get(chash, ""))
            ts_span = f' <span class="criteria-group-time">{ts}</span>' if ts else ''
            grouped_items += f'<div class="criteria-group-header"><span class="criteria-group-icon">&#9654;</span> {commit_link}{ts_span} &mdash; <span class="criteria-group-count">{counts["done"]}/{counts["total"]} done</span>{cost_badge}{token_badge}</div>'
        else:
            grouped_items += f'<div class="criteria-group-header"><span class="criteria-group-icon">&#9654;</span> <span class="criteria-group-name">Uncommitted</span> &mdash; <span class="criteria-group-count">{counts["done"]}/{counts["total"]} done</span>{cost_badge}{token_badge}</div>'
        grouped_items += '<div class="criteria-group-items">'
        grouped_items += ''.join(commit_groups_html[chash])
        grouped_items += '</div></div>\n'

    sort_bar = (
        '<div class="criteria-sort-bar">'
        '<button class="criteria-view-toggle active" data-view="grouped" title="Switch between grouped and flat views">Grouped</button>'
        '<span class="criteria-sort-sep"></span>'
        '<span class="criteria-sort-label">Sort:</span>'
        '<button class="criteria-sort-btn" data-sort-key="completed">Completed <span class="sort-arrow">&#9650;</span></button>'
        '<button class="criteria-sort-btn" data-sort-key="cost">Cost <span class="sort-arrow">&#9650;</span></button>'
        '<button class="criteria-sort-btn" data-sort-key="commit">Commit <span class="sort-arrow">&#9650;</span></button>'
        '</div>'
    )
    criteria_header = (
        '<div class="criteria-header">'
        '<span class="criterion-id">ID</span>'
        '<span class="criteria-header-status">Status</span>'
        '<span class="criterion-text">Criterion</span>'
        '<span class="criterion-badges">'
        '<span class="criteria-header-label">Type</span>'
        '<span class="criteria-header-label">Cost</span>'
        '<span class="criteria-header-label">Commit</span>'
        '<span class="criteria-header-label">Completed At</span>'
        '</span></div>'
    )

    return (
        f'<tr class="criteria-row" data-parent="{tid}" style="display:none">\n'
        f'  <td colspan="13"><div class="criteria-detail">'
        f'{sort_bar}{criteria_header}'
        f'<div class="criteria-grouped-view">{grouped_items}</div>'
        f'<div class="criteria-flat-view" style="display:none">{flat_items}</div>'
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
  var sortCol = 12;
  var sortAsc = false;
  var statusFilter = 'All';
  var searchTerm = '';

  var headers = document.querySelectorAll('#metricsTable thead th');
  var chips = document.querySelectorAll('#statusFilters .filter-chip');
  var searchInput = document.getElementById('searchInput');
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
      if (searchTerm && row.getAttribute('data-summary').indexOf(searchTerm) === -1) return false;
      return true;
    });
    currentPage = 1;
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
    render();
  }

  function updateFooter() {
    var totalSessions = 0, totalDuration = 0, totalLines = 0;
    var totalIn = 0, totalOut = 0, totalCost = 0, count = 0;
    filtered.forEach(function(row) {
      totalSessions += parseFloat(row.children[6].getAttribute('data-sort')) || 0;
      totalDuration += parseFloat(row.children[7].getAttribute('data-sort')) || 0;
      totalLines += parseFloat(row.children[8].getAttribute('data-sort')) || 0;
      totalIn += parseFloat(row.children[9].getAttribute('data-sort')) || 0;
      totalOut += parseFloat(row.children[10].getAttribute('data-sort')) || 0;
      totalCost += parseFloat(row.children[11].getAttribute('data-sort')) || 0;
      count++;
    });
    var label = statusFilter === 'All' && !searchTerm ? 'Total' : 'Filtered total (' + count + ' tasks)';
    footerLabel.textContent = label;
    footerSessions.textContent = totalSessions;
    footerDuration.textContent = formatDuration(totalDuration);
    footerLines.textContent = totalLines > 0 ? totalLines : '\\u2014';
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

  // Expand/collapse criteria rows
  body.addEventListener('click', function(e) {
    var row = e.target.closest('tr.expandable');
    if (!row) return;
    var tid = row.getAttribute('data-task-id');
    var detail = body.querySelector('tr.criteria-row[data-parent="' + tid + '"]');
    if (!detail) return;
    var isExpanded = row.classList.toggle('expanded');
    detail.style.display = isExpanded ? '' : 'none';
  });

  // Criteria view toggle (grouped <-> flat)
  document.addEventListener('click', function(e) {
    var toggle = e.target.closest('.criteria-view-toggle');
    if (!toggle) return;
    e.stopPropagation();
    var detail = toggle.closest('.criteria-detail');
    if (!detail) return;
    var groupedView = detail.querySelector('.criteria-grouped-view');
    var flatView = detail.querySelector('.criteria-flat-view');
    var isGrouped = toggle.getAttribute('data-view') === 'grouped';
    if (isGrouped) {
      toggle.setAttribute('data-view', 'flat');
      toggle.textContent = 'Flat';
      toggle.classList.remove('active');
      groupedView.style.display = 'none';
      flatView.style.display = '';
    } else {
      toggle.setAttribute('data-view', 'grouped');
      toggle.textContent = 'Grouped';
      toggle.classList.add('active');
      groupedView.style.display = '';
      flatView.style.display = 'none';
    }
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
    var sortKey = btn.getAttribute('data-sort-key');
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
    if (!wasAsc && !wasDesc) {
      dir = 'asc';
    } else if (wasAsc) {
      dir = 'desc';
    } else {
      dir = 'none';
    }

    if (dir !== 'none') {
      btn.classList.add(dir === 'asc' ? 'sort-asc' : 'sort-desc');
      btn.querySelector('.sort-arrow').textContent = dir === 'asc' ? '\u25B2' : '\u25BC';
    }

    function sortItems(container) {
      var items = Array.prototype.slice.call(container.querySelectorAll(':scope > .criterion-item'));
      if (dir === 'none') {
        items.sort(function(a, b) {
          var idA = parseInt(a.querySelector('.criterion-id').textContent.replace('#', ''));
          var idB = parseInt(b.querySelector('.criterion-id').textContent.replace('#', ''));
          return idA - idB;
        });
      } else {
        var attrName = 'data-sort-' + sortKey;
        var isNumeric = (sortKey === 'cost');
        items.sort(function(a, b) {
          var vA = a.getAttribute(attrName) || '';
          var vB = b.getAttribute(attrName) || '';
          var cmp;
          if (isNumeric) {
            cmp = (parseFloat(vA) || 0) - (parseFloat(vB) || 0);
          } else {
            cmp = vA.localeCompare(vB);
          }
          return dir === 'asc' ? cmp : -cmp;
        });
      }
      items.forEach(function(item) { container.appendChild(item); });
    }

    var groupContainers = detail.querySelectorAll('.criteria-group-items');
    groupContainers.forEach(function(gc) { sortItems(gc); });

    var flatView = detail.querySelector('.criteria-flat-view');
    if (flatView) { sortItems(flatView); }
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

  // Search input
  searchInput.addEventListener('input', function() {
    searchTerm = searchInput.value.toLowerCase();
    applyFilter();
  });

  // Page size
  pageSizeEl.addEventListener('change', function() {
    pageSize = parseInt(pageSizeEl.value);
    currentPage = 1;
    render();
  });

  // Prev/Next
  prevBtn.addEventListener('click', function() {
    if (currentPage > 1) { currentPage--; render(); }
  });
  nextBtn.addEventListener('click', function() {
    var maxP = Math.ceil(filtered.length / pageSize);
    if (currentPage < maxP) { currentPage++; render(); }
  });

  // Initial render
  applySort();

  // Cost trend tab switching
  var costTabs = document.querySelectorAll('#costTrendTabs .cost-tab');
  var costViews = document.querySelectorAll('.cost-trend-view');
  costTabs.forEach(function(tab) {
    tab.addEventListener('click', function() {
      var target = tab.getAttribute('data-tab');
      costTabs.forEach(function(t) { t.classList.remove('active'); });
      tab.classList.add('active');
      costViews.forEach(function(v) {
        v.style.display = v.getAttribute('data-view') === target ? '' : 'none';
      });
    });
  });

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
      chips.forEach(function(c) { c.classList.remove('active'); });
      document.querySelector('.filter-chip[data-filter="All"]').classList.add('active');
      statusFilter = 'All';
      searchInput.value = '';
      searchTerm = '';
      applyFilter();
    }
    targetRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
    targetRow.classList.add('dep-highlight');
    setTimeout(function() { targetRow.classList.remove('dep-highlight'); }, 2000);
  });
})();
</script>"""


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def generate_html(task_metrics: list[dict], complexity_metrics: list[dict] = None,
                  cost_trend: list[dict] = None, all_criteria: dict[int, list[dict]] = None,
                  cost_trend_daily: list[dict] = None, cost_trend_monthly: list[dict] = None,
                  task_deps: dict[int, dict] = None, kpi_data: dict = None) -> str:
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

    # KPI cards
    kpi_html = generate_kpi_cards(kpi_data) if kpi_data else ""

    # Charts
    charts_html = generate_charts_section(
        cost_trend or [], cost_trend_daily or [], cost_trend_monthly or []
    )

    # Complexity
    complexity_html = generate_complexity_section(complexity_metrics)

    css = generate_css()
    header = generate_header(now)
    filter_bar = generate_filter_bar()
    table_header = generate_table_header()
    table_footer = generate_table_footer(total_sessions, total_duration, total_lines_added,
                                         total_lines_removed, total_tokens_in, total_tokens_out, total_cost)
    pagination = generate_pagination()
    js = generate_js()

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tusk &mdash; Task Metrics</title>
<style>
{css}
</style>
</head>
<body>

{header}

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
    conn.close()

    log.debug("Cost by domain: %s", cost_by_domain)

    # Generate HTML
    html_content = generate_html(
        task_metrics, complexity_metrics, cost_trend, all_criteria,
        cost_trend_daily, cost_trend_monthly, task_deps, kpi_data
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
