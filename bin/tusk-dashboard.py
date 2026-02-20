#!/usr/bin/env python3
"""Generate a static HTML dashboard for tusk task databases.

Currently displays per-task metrics: token counts and monetary cost.

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
from datetime import datetime

log = logging.getLogger(__name__)

# Expected session ranges per complexity tier (from CLAUDE.md)
EXPECTED_SESSIONS = {
    'XS': (0.5, 1),
    'S': (1, 1.5),
    'M': (1, 2),
    'L': (3, 5),
    'XL': (5, 10),
}


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_task_metrics(conn: sqlite3.Connection) -> list[dict]:
    """Fetch per-task token and cost metrics from task_metrics view."""
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
                  s.model,
                  tm.created_at,
                  tm.updated_at
           FROM task_metrics tm
           LEFT JOIN task_sessions s ON s.id = (
               SELECT s2.id FROM task_sessions s2
               WHERE s2.task_id = tm.id
               ORDER BY s2.cost_dollars DESC
               LIMIT 1
           )
           ORDER BY tm.total_cost DESC, tm.id ASC"""
    ).fetchall()
    result = [dict(r) for r in rows]
    log.debug("Fetched %d task metrics rows", len(result))
    return result


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


def fetch_all_criteria(conn: sqlite3.Connection) -> dict[int, list[dict]]:
    """Fetch all acceptance criteria, grouped by task_id."""
    log.debug("Querying acceptance_criteria table")
    rows = conn.execute(
        """SELECT id, task_id, criterion, is_completed, source, cost_dollars, completed_at, criterion_type, commit_hash
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


def generate_cost_trend_svg(cost_trend: list[dict]) -> str:
    """Generate an inline SVG bar chart with cumulative cost line.

    Returns the full <svg> element as a string, or an empty-state message
    if there is no data.
    """
    if not cost_trend:
        return '<p class="empty">No session cost data available yet.</p>'

    # Chart dimensions
    chart_w = 800
    chart_h = 260
    pad_left = 70
    pad_right = 20
    pad_top = 20
    pad_bottom = 60

    plot_w = chart_w - pad_left - pad_right
    plot_h = chart_h - pad_top - pad_bottom

    weeks = [row["week_start"] for row in cost_trend]
    costs = [row["weekly_cost"] for row in cost_trend]
    n = len(weeks)

    # Cumulative costs
    cumulative = []
    running = 0.0
    for c in costs:
        running += c
        cumulative.append(running)

    max_cost = max(costs) if costs else 1
    max_cumulative = cumulative[-1] if cumulative else 1
    # Avoid division by zero
    if max_cost == 0:
        max_cost = 1
    if max_cumulative == 0:
        max_cumulative = 1

    bar_width = max(4, min(40, (plot_w / n) * 0.6))
    bar_gap = plot_w / n

    # Build bars
    bars = []
    for i, cost in enumerate(costs):
        x = pad_left + i * bar_gap + (bar_gap - bar_width) / 2
        bar_h = (cost / max_cost) * plot_h
        y = pad_top + plot_h - bar_h
        tooltip = f"Week of {weeks[i]}: ${cost:,.2f}"
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" '
            f'height="{bar_h:.1f}" fill="var(--accent)" opacity="0.7" rx="2">'
            f"<title>{esc(tooltip)}</title></rect>"
        )

    # Build cumulative line
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
    # Dots on the cumulative line
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

    # Y-axis labels for bar scale (left)
    y_labels = []
    for frac in [0, 0.25, 0.5, 0.75, 1.0]:
        val = max_cost * frac
        y = pad_top + plot_h - frac * plot_h
        y_labels.append(
            f'<text x="{pad_left - 8}" y="{y:.1f}" '
            f'text-anchor="end" dominant-baseline="middle" '
            f'fill="var(--text-muted)" font-size="11">${val:,.0f}</text>'
        )
        # Grid line
        y_labels.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" '
            f'x2="{chart_w - pad_right}" y2="{y:.1f}" '
            f'stroke="var(--border)" stroke-dasharray="3,3"/>'
        )

    # Y-axis labels for cumulative scale (right)
    cum_labels = []
    for frac in [0, 0.5, 1.0]:
        val = max_cumulative * frac
        y = pad_top + plot_h - frac * plot_h
        cum_labels.append(
            f'<text x="{chart_w - pad_right + 8}" y="{y:.1f}" '
            f'text-anchor="start" dominant-baseline="middle" '
            f'fill="#f59e0b" font-size="11">${val:,.0f}</text>'
        )

    # X-axis labels (show a subset if too many weeks)
    x_labels = []
    step = max(1, n // 10)
    for i in range(0, n, step):
        x = pad_left + i * bar_gap + bar_gap / 2
        # Format as Mon DD
        try:
            dt = datetime.strptime(weeks[i], "%Y-%m-%d")
            label = dt.strftime("%b %d")
        except ValueError:
            label = weeks[i]
        x_labels.append(
            f'<text x="{x:.1f}" y="{pad_top + plot_h + 20}" '
            f'text-anchor="middle" fill="var(--text-muted)" '
            f'font-size="11">{esc(label)}</text>'
        )

    # Axis label for cumulative line
    right_pad = pad_right + 50

    svg = f"""<svg viewBox="0 0 {chart_w + 50} {chart_h}" xmlns="http://www.w3.org/2000/svg"
     style="width:100%;max-width:{chart_w + 50}px;height:auto;font-family:inherit;">
  {''.join(y_labels)}
  {''.join(cum_labels)}
  {''.join(bars)}
  {polyline}
  {''.join(dots)}
  {''.join(x_labels)}
  <!-- Legend -->
  <rect x="{pad_left}" y="{chart_h - 12}" width="12" height="12" fill="var(--accent)" opacity="0.7" rx="2"/>
  <text x="{pad_left + 16}" y="{chart_h - 1}" fill="var(--text-muted)" font-size="11">Weekly cost</text>
  <line x1="{pad_left + 110}" y1="{chart_h - 6}" x2="{pad_left + 130}" y2="{chart_h - 6}" stroke="#f59e0b" stroke-width="2.5"/>
  <circle cx="{pad_left + 120}" cy="{chart_h - 6}" r="3" fill="#f59e0b"/>
  <text x="{pad_left + 135}" y="{chart_h - 1}" fill="#f59e0b" font-size="11">Cumulative</text>
</svg>"""
    return svg


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


def generate_html(task_metrics: list[dict], complexity_metrics: list[dict] = None, cost_trend: list[dict] = None, all_criteria: dict[int, list[dict]] = None) -> str:
    """Generate the full HTML dashboard."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Totals row
    total_tokens_in = sum(t["total_tokens_in"] for t in task_metrics)
    total_tokens_out = sum(t["total_tokens_out"] for t in task_metrics)
    total_cost = sum(t["total_cost"] for t in task_metrics)

    # Task rows — include data attributes for JS filtering/sorting
    if all_criteria is None:
        all_criteria = {}

    # Build task_id → github_pr map for commit hash links in criteria
    task_pr_map: dict[int, str] = {}
    for t in task_metrics:
        if t.get("github_pr"):
            task_pr_map[t["id"]] = t["github_pr"]

    task_rows = ""
    for t in task_metrics:
        has_data = t["session_count"] > 0
        status_val = esc(t['status'])
        tid = t['id']
        criteria_list = all_criteria.get(tid, [])
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
        task_rows += f"""<tr{cls_attr} data-status="{status_val}" data-summary="{esc(t['summary']).lower()}" data-task-id="{tid}">
  <td class="col-id" data-sort="{tid}">{toggle_icon}#{tid}</td>
  <td class="col-summary">{esc(t['summary'])}</td>
  <td class="col-status"><span class="status-badge status-{status_val.lower().replace(' ', '-')}">{status_val}</span></td>
  <td class="col-wsjf" data-sort="{priority_score}">{priority_score}</td>
  <td class="col-complexity">{f'<span class="complexity-badge">{complexity_val}</span>' if complexity_val else ''}</td>
  <td class="col-date" data-sort="{esc(t.get('created_at') or '')}">{format_date(t.get('created_at'))}</td>
  <td class="col-date" data-sort="{esc(t.get('updated_at') or '')}">{format_date(t.get('updated_at'))}</td>
  <td class="col-model">{esc(t.get('model') or '')}</td>
  <td class="col-tokens-in" data-sort="{t['total_tokens_in']}">{format_number(t['total_tokens_in'])}</td>
  <td class="col-tokens-out" data-sort="{t['total_tokens_out']}">{format_number(t['total_tokens_out'])}</td>
  <td class="col-cost" data-sort="{t['total_cost']}">{format_cost(t['total_cost'])}</td>
</tr>\n"""
        # Criteria detail row (hidden by default)
        if has_criteria:
            criteria_items = ""
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
                    pr_url = task_pr_map.get(tid, "")
                    if pr_url and "/pull/" in pr_url:
                        repo_url = pr_url.split("/pull/")[0]
                        commit_url = f"{repo_url}/commit/{esc(cr['commit_hash'])}"
                        commit_badge = f' <a href="{commit_url}" class="criterion-commit" target="_blank">{esc(cr["commit_hash"])}</a>'
                    else:
                        commit_badge = f' <span class="criterion-commit">{esc(cr["commit_hash"])}</span>'
                badges = f'<span class="criterion-badges">{type_badge}{source_badge}{cost_badge}{commit_badge}{time_badge}</span>'
                sort_completed = esc(cr.get("completed_at") or "")
                sort_cost = cr.get("cost_dollars") or 0
                sort_type = esc(ctype)
                criteria_items += f'<div class="criterion-item {css}" data-sort-completed="{sort_completed}" data-sort-cost="{sort_cost}" data-sort-type="{sort_type}"><span class="criterion-id">#{cr["id"]}</span> {check} <span class="criterion-text">{esc(cr["criterion"])}</span>{badges}</div>\n'

            sort_bar = """<div class="criteria-sort-bar"><span class="criteria-sort-label">Sort:</span><button class="criteria-sort-btn" data-sort-key="completed">Completed <span class="sort-arrow">&#9650;</span></button><button class="criteria-sort-btn" data-sort-key="cost">Cost <span class="sort-arrow">&#9650;</span></button><button class="criteria-sort-btn" data-sort-key="type">Type <span class="sort-arrow">&#9650;</span></button></div>"""
            criteria_header = """<div class="criteria-header"><span class="criterion-id">ID</span><span class="criteria-header-status">Status</span><span class="criterion-text">Criterion</span><span class="criterion-badges"><span class="criteria-header-label">Type</span><span class="criteria-header-label">Cost</span><span class="criteria-header-label">Commit</span><span class="criteria-header-label">Completed At</span></span></div>"""
            task_rows += f"""<tr class="criteria-row" data-parent="{tid}" style="display:none">
  <td colspan="11"><div class="criteria-detail">{sort_bar}{criteria_header}{criteria_items}</div></td>
</tr>\n"""

    # Empty state
    if not task_metrics:
        task_rows = '<tr><td colspan="11" class="empty">No tasks found. Run <code>tusk init</code> and add some tasks.</td></tr>'

    # Complexity metrics section
    complexity_section = ""
    if complexity_metrics:
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
        complexity_section = f"""
  <div class="panel" style="margin-top: 1.5rem;">
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

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tusk — Task Metrics</title>
<style>
:root {{
  --bg: #f8fafc;
  --bg-panel: #ffffff;
  --text: #0f172a;
  --text-muted: #94a3b8;
  --border: #e2e8f0;
  --accent: #3b82f6;
  --accent-light: #dbeafe;
  --shadow: 0 1px 3px rgba(0,0,0,0.08);
  --hover: #f1f5f9;
}}

@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0f172a;
    --bg-panel: #1e293b;
    --text: #f1f5f9;
    --text-muted: #64748b;
    --border: #334155;
    --accent: #60a5fa;
    --accent-light: #1e3a5f;
    --shadow: 0 1px 3px rgba(0,0,0,0.3);
    --hover: #334155;
  }}
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
}}

.header {{
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border);
  padding: 1rem 2rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
  box-shadow: var(--shadow);
}}

.header h1 {{
  font-size: 1.5rem;
  font-weight: 700;
}}

.header .timestamp {{
  color: var(--text-muted);
  font-size: 0.85rem;
}}

.container {{
  max-width: 1200px;
  margin: 0 auto;
  padding: 1.5rem;
}}

/* Table */
.panel {{
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow);
  overflow-x: auto;
}}

table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.875rem;
}}

thead th {{
  text-align: left;
  padding: 0.75rem 1rem;
  border-bottom: 2px solid var(--border);
  font-weight: 600;
  color: var(--text-muted);
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  white-space: nowrap;
  cursor: pointer;
  user-select: none;
  position: relative;
}}

thead th .sort-arrow {{
  display: inline-block;
  margin-left: 0.3em;
  font-size: 0.65rem;
  opacity: 0.3;
}}

thead th.sort-asc .sort-arrow,
thead th.sort-desc .sort-arrow {{
  opacity: 1;
  color: var(--accent);
}}

tbody td {{
  padding: 0.6rem 1rem;
  border-bottom: 1px solid var(--border);
}}

tbody tr:last-child td {{
  border-bottom: none;
}}

tbody tr:hover {{
  background: var(--hover);
}}

tr.muted td {{
  color: var(--text-muted);
}}

tfoot td {{
  padding: 0.75rem 1rem;
  border-top: 2px solid var(--border);
  font-weight: 700;
  font-size: 0.875rem;
}}

.col-id {{
  white-space: nowrap;
  color: var(--text-muted);
  font-weight: 600;
  font-size: 0.8rem;
}}

.col-summary {{
  max-width: 400px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}

.col-model {{
  font-size: 0.8rem;
  color: var(--text-muted);
  white-space: nowrap;
}}

.col-date {{
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
  font-size: 0.8rem;
  color: var(--text-muted);
}}

.col-wsjf {{
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  font-weight: 600;
  font-size: 0.8rem;
}}

.col-tokens-in,
.col-tokens-out,
.col-cost {{
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}}

.status-badge {{
  font-size: 0.7rem;
  font-weight: 600;
  padding: 0.15rem 0.5rem;
  border-radius: 4px;
  white-space: nowrap;
}}

.status-to-do {{
  background: var(--accent-light);
  color: var(--accent);
}}

.status-in-progress {{
  background: #fef3c7;
  color: #d97706;
}}

.status-done {{
  background: #dcfce7;
  color: #16a34a;
}}

@media (prefers-color-scheme: dark) {{
  .status-in-progress {{
    background: #78350f;
    color: #fbbf24;
  }}
  .status-done {{
    background: #14532d;
    color: #4ade80;
  }}
}}

.empty {{
  text-align: center;
  padding: 2rem 1rem;
  color: var(--text-muted);
}}

.empty code {{
  background: var(--hover);
  padding: 0.15rem 0.4rem;
  border-radius: 3px;
  font-size: 0.85em;
}}

.section-header {{
  padding: 0.75rem 1rem;
  font-weight: 700;
  font-size: 0.875rem;
  border-bottom: 1px solid var(--border);
}}

.col-complexity {{
  white-space: nowrap;
  font-weight: 600;
}}

.complexity-badge {{
  font-size: 0.75rem;
  font-weight: 700;
  padding: 0.15rem 0.5rem;
  border-radius: 4px;
  background: var(--accent-light);
  color: var(--accent);
}}

.col-count,
.col-avg-sessions,
.col-avg-duration,
.col-avg-cost {{
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}}

.col-expected {{
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  color: var(--text-muted);
  font-size: 0.8rem;
}}

.tier-exceeds {{
  background: #fef2f2;
}}

.tier-exceeds .col-avg-sessions {{
  color: #dc2626;
  font-weight: 700;
}}

.tier-flag {{
  font-size: 0.75rem;
}}

.text-muted-dash {{
  color: var(--text-muted);
}}

@media (prefers-color-scheme: dark) {{
  .tier-exceeds {{
    background: #7f1d1d;
  }}
  .tier-exceeds .col-avg-sessions {{
    color: #fca5a5;
  }}
}}

/* Collapsible criteria rows */
tr.expandable {{
  cursor: pointer;
}}

tr.expandable:hover .expand-icon {{
  color: var(--accent);
}}

.expand-icon {{
  display: inline-block;
  font-size: 0.6rem;
  transition: transform 0.15s;
  color: var(--text-muted);
}}

tr.expandable.expanded .expand-icon {{
  transform: rotate(90deg);
}}

.criteria-row td {{
  padding: 0 !important;
  border-bottom: 1px solid var(--border);
}}

.criteria-detail {{
  padding: 0.5rem 1rem 0.5rem 2.5rem;
  background: var(--bg);
}}

.criterion-item {{
  padding: 0.25rem 0;
  font-size: 0.8rem;
  display: flex;
  align-items: baseline;
  gap: 0.4rem;
}}

.criteria-header {{
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
}}

.criteria-header-status {{
  width: 1em;
  text-align: center;
}}

.criteria-header-label {{
  font-size: 0.65rem;
  font-weight: 700;
  padding: 0.1rem 0.35rem;
  color: var(--text-muted);
}}

.criterion-done {{
  color: #16a34a;
}}

.criterion-pending {{
  color: var(--text-muted);
}}

.criterion-id {{
  font-size: 0.7rem;
  font-weight: 600;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
  min-width: 2.5em;
}}

.criterion-text {{
  flex: 1;
  min-width: 0;
}}

.criterion-badges {{
  display: flex;
  gap: 0.3rem;
  margin-left: auto;
  flex-shrink: 0;
}}

.criterion-source {{
  font-size: 0.65rem;
  font-weight: 600;
  padding: 0.1rem 0.35rem;
  border-radius: 3px;
  background: var(--hover);
  color: var(--text-muted);
}}

.criterion-cost {{
  font-size: 0.65rem;
  font-weight: 600;
  padding: 0.1rem 0.35rem;
  border-radius: 3px;
  background: #dcfce7;
  color: #166534;
  font-variant-numeric: tabular-nums;
}}

.criterion-time {{
  font-size: 0.65rem;
  font-weight: 600;
  padding: 0.1rem 0.35rem;
  border-radius: 3px;
  background: #dbeafe;
  color: #1e40af;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}}

.criterion-commit {{
  font-size: 0.65rem;
  font-weight: 600;
  padding: 0.1rem 0.35rem;
  border-radius: 3px;
  background: #fef3c7;
  color: #92400e;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  font-variant-numeric: tabular-nums;
  text-decoration: none;
  white-space: nowrap;
}}

a.criterion-commit:hover {{
  background: #fde68a;
  text-decoration: underline;
}}

@media (prefers-color-scheme: dark) {{
  .criterion-commit {{
    background: #78350f;
    color: #fbbf24;
  }}
  a.criterion-commit:hover {{
    background: #92400e;
  }}
}}

@media (prefers-color-scheme: dark) {{
  .criterion-cost {{
    background: #14532d;
    color: #86efac;
  }}
  .criterion-time {{
    background: #1e3a5f;
    color: #93c5fd;
  }}
}}

.criterion-type {{
  font-size: 0.65rem;
  font-weight: 600;
  padding: 0.1rem 0.35rem;
  border-radius: 3px;
  background: #f3e8ff;
  color: #7c3aed;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}}

.criterion-type-code {{
  background: #fef3c7;
  color: #d97706;
}}

.criterion-type-test {{
  background: #dcfce7;
  color: #16a34a;
}}

.criterion-type-file {{
  background: #dbeafe;
  color: #1e40af;
}}

@media (prefers-color-scheme: dark) {{
  .criterion-type {{
    background: #4c1d95;
    color: #c4b5fd;
  }}
  .criterion-type-code {{
    background: #78350f;
    color: #fbbf24;
  }}
  .criterion-type-test {{
    background: #14532d;
    color: #86efac;
  }}
  .criterion-type-file {{
    background: #1e3a5f;
    color: #93c5fd;
  }}
}}

/* Criteria sort bar */
.criteria-sort-bar {{
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.3rem 0;
  margin-bottom: 0.3rem;
  border-bottom: 1px solid var(--border);
}}

.criteria-sort-label {{
  font-size: 0.7rem;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-right: 0.2rem;
}}

.criteria-sort-btn {{
  font-size: 0.7rem;
  font-weight: 600;
  padding: 0.15rem 0.45rem;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  user-select: none;
  transition: all 0.15s;
  white-space: nowrap;
}}

.criteria-sort-btn:hover {{
  border-color: var(--accent);
  color: var(--accent);
}}

.criteria-sort-btn .sort-arrow {{
  display: inline-block;
  margin-left: 0.2em;
  font-size: 0.55rem;
  opacity: 0.3;
}}

.criteria-sort-btn.sort-asc .sort-arrow,
.criteria-sort-btn.sort-desc .sort-arrow {{
  opacity: 1;
  color: var(--accent);
}}

.criterion-empty {{
  font-size: 0.8rem;
  color: var(--text-muted);
  font-style: italic;
}}

/* Filter bar */
.filter-bar {{
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.75rem 1rem;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}}

.filter-chips {{
  display: flex;
  gap: 0.35rem;
}}

.filter-chip {{
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.25rem 0.65rem;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: all 0.15s;
}}

.filter-chip:hover {{
  border-color: var(--accent);
  color: var(--accent);
}}

.filter-chip.active {{
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}}

.search-input {{
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
}}

.search-input:focus {{
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-light);
}}

.search-input::placeholder {{
  color: var(--text-muted);
}}

/* Pagination */
.pagination-bar {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.6rem 1rem;
  border-top: 1px solid var(--border);
  font-size: 0.8rem;
  color: var(--text-muted);
}}

.pagination-bar .page-info {{
  font-variant-numeric: tabular-nums;
}}

.pagination-controls {{
  display: flex;
  align-items: center;
  gap: 0.5rem;
}}

.page-size-select {{
  padding: 0.2rem 0.4rem;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg);
  color: var(--text);
  font-size: 0.8rem;
  cursor: pointer;
}}

.page-btn {{
  padding: 0.25rem 0.6rem;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: transparent;
  color: var(--text);
  font-size: 0.8rem;
  cursor: pointer;
}}

.page-btn:hover:not(:disabled) {{
  background: var(--hover);
  border-color: var(--accent);
}}

.page-btn:disabled {{
  opacity: 0.35;
  cursor: default;
}}

@media (max-width: 700px) {{
  .col-summary {{
    max-width: 180px;
  }}
}}
</style>
</head>
<body>

<div class="header">
  <h1>Tusk — Task Metrics</h1>
  <span class="timestamp">Generated {esc(now)}</span>
</div>

<div class="container">
  <div class="panel">
    <div class="filter-bar">
      <div class="filter-chips" id="statusFilters">
        <button class="filter-chip active" data-filter="All">All</button>
        <button class="filter-chip" data-filter="To Do">To Do</button>
        <button class="filter-chip" data-filter="In Progress">In Progress</button>
        <button class="filter-chip" data-filter="Done">Done</button>
      </div>
      <input type="text" class="search-input" id="searchInput" placeholder="Search tasks\u2026">
    </div>
    <table id="metricsTable">
      <thead>
        <tr>
          <th data-col="0" data-type="num">ID <span class="sort-arrow">\u25B2</span></th>
          <th data-col="1" data-type="str">Task <span class="sort-arrow">\u25B2</span></th>
          <th data-col="2" data-type="str">Status <span class="sort-arrow">\u25B2</span></th>
          <th data-col="3" data-type="num" style="text-align:right">WSJF <span class="sort-arrow">\u25B2</span></th>
          <th data-col="4" data-type="str">Size <span class="sort-arrow">\u25B2</span></th>
          <th data-col="5" data-type="str">Started <span class="sort-arrow">\u25B2</span></th>
          <th data-col="6" data-type="str" class="sort-desc">Last Updated <span class="sort-arrow">\u25BC</span></th>
          <th data-col="7" data-type="str">Model <span class="sort-arrow">\u25B2</span></th>
          <th data-col="8" data-type="num" style="text-align:right">Tokens In <span class="sort-arrow">\u25B2</span></th>
          <th data-col="9" data-type="num" style="text-align:right">Tokens Out <span class="sort-arrow">\u25B2</span></th>
          <th data-col="10" data-type="num" style="text-align:right">Cost <span class="sort-arrow">\u25B2</span></th>
        </tr>
      </thead>
      <tbody id="metricsBody">
        {task_rows}
      </tbody>
      <tfoot>
        <tr>
          <td colspan="8" id="footerLabel">Total</td>
          <td class="col-tokens-in" id="footerTokensIn">{format_number(total_tokens_in)}</td>
          <td class="col-tokens-out" id="footerTokensOut">{format_number(total_tokens_out)}</td>
          <td class="col-cost" id="footerCost">{format_cost(total_cost)}</td>
        </tr>
      </tfoot>
    </table>
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
    </div>
  </div>
  <div class="panel" style="margin-top: 1.5rem;">
    <div class="section-header">Cost Trend</div>
    <div style="padding: 1rem;">
      {generate_cost_trend_svg(cost_trend if cost_trend is not None else [])}
    </div>
  </div>{complexity_section}
</div>

<script>
(function() {{
  var body = document.getElementById('metricsBody');
  if (!body) return;
  var allRows = Array.prototype.slice.call(body.querySelectorAll('tr:not(.criteria-row)'));
  var criteriaRows = {{}};
  body.querySelectorAll('tr.criteria-row').forEach(function(cr) {{
    criteriaRows[cr.getAttribute('data-parent')] = cr;
  }});
  var filtered = allRows.slice();
  var currentPage = 1;
  var pageSize = 25;
  var sortCol = 6;
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
  var footerIn = document.getElementById('footerTokensIn');
  var footerOut = document.getElementById('footerTokensOut');
  var footerCost = document.getElementById('footerCost');

  function formatNum(n) {{
    return n.toString().replace(/\\B(?=(\\d{{3}})+(?!\\d))/g, ',');
  }}

  function formatCost(n) {{
    return '$' + n.toFixed(2).replace(/\\B(?=(\\d{{3}})+(?!\\d))/g, ',');
  }}

  function applyFilter() {{
    filtered = allRows.filter(function(row) {{
      if (statusFilter !== 'All' && row.getAttribute('data-status') !== statusFilter) return false;
      if (searchTerm && row.getAttribute('data-summary').indexOf(searchTerm) === -1) return false;
      return true;
    }});
    currentPage = 1;
    render();
  }}

  function applySort() {{
    if (sortCol < 0) return;
    var type = headers[sortCol].getAttribute('data-type');
    filtered.sort(function(a, b) {{
      var cellA = a.children[sortCol];
      var cellB = b.children[sortCol];
      var vA, vB;
      if (type === 'num') {{
        vA = parseFloat(cellA.getAttribute('data-sort')) || 0;
        vB = parseFloat(cellB.getAttribute('data-sort')) || 0;
      }} else {{
        vA = (cellA.textContent || '').toLowerCase();
        vB = (cellB.textContent || '').toLowerCase();
      }}
      if (vA < vB) return sortAsc ? -1 : 1;
      if (vA > vB) return sortAsc ? 1 : -1;
      return 0;
    }});
    render();
  }}

  function updateFooter() {{
    var totalIn = 0, totalOut = 0, totalCost = 0, count = 0;
    filtered.forEach(function(row) {{
      totalIn += parseFloat(row.children[8].getAttribute('data-sort')) || 0;
      totalOut += parseFloat(row.children[9].getAttribute('data-sort')) || 0;
      totalCost += parseFloat(row.children[10].getAttribute('data-sort')) || 0;
      count++;
    }});
    var label = statusFilter === 'All' && !searchTerm ? 'Total' : 'Filtered total (' + count + ' tasks)';
    footerLabel.textContent = label;
    footerIn.textContent = formatNum(totalIn);
    footerOut.textContent = formatNum(totalOut);
    footerCost.textContent = formatCost(totalCost);
  }}

  function render() {{
    // Hide all rows first (task rows and criteria rows)
    allRows.forEach(function(r) {{ r.style.display = 'none'; }});
    Object.keys(criteriaRows).forEach(function(k) {{ criteriaRows[k].style.display = 'none'; }});

    var start, end;
    if (pageSize === 0) {{
      start = 0;
      end = filtered.length;
    }} else {{
      var maxPage = Math.max(1, Math.ceil(filtered.length / pageSize));
      if (currentPage > maxPage) currentPage = maxPage;
      start = (currentPage - 1) * pageSize;
      end = Math.min(start + pageSize, filtered.length);
    }}

    // Show visible rows in sorted order, keeping criteria rows after parents
    for (var i = 0; i < filtered.length; i++) {{
      body.appendChild(filtered[i]);
      var tid = filtered[i].getAttribute('data-task-id');
      if (tid && criteriaRows[tid]) {{
        body.appendChild(criteriaRows[tid]);
      }}
    }}
    for (var j = start; j < end; j++) {{
      filtered[j].style.display = '';
      // Show criteria row only if parent is expanded and visible
      var jtid = filtered[j].getAttribute('data-task-id');
      if (jtid && criteriaRows[jtid] && filtered[j].classList.contains('expanded')) {{
        criteriaRows[jtid].style.display = '';
      }}
    }}

    // Pagination info
    if (pageSize === 0) {{
      pageInfo.textContent = filtered.length + ' tasks';
      prevBtn.disabled = true;
      nextBtn.disabled = true;
    }} else {{
      var maxP = Math.max(1, Math.ceil(filtered.length / pageSize));
      pageInfo.textContent = 'Page ' + currentPage + ' of ' + maxP + ' (' + filtered.length + ' tasks)';
      prevBtn.disabled = currentPage <= 1;
      nextBtn.disabled = currentPage >= maxP;
    }}

    updateFooter();
  }}

  // Expand/collapse criteria rows
  body.addEventListener('click', function(e) {{
    var row = e.target.closest('tr.expandable');
    if (!row) return;
    var tid = row.getAttribute('data-task-id');
    var detail = body.querySelector('tr.criteria-row[data-parent="' + tid + '"]');
    if (!detail) return;
    var isExpanded = row.classList.toggle('expanded');
    detail.style.display = isExpanded ? '' : 'none';
  }});

  // Criteria sort buttons
  document.addEventListener('click', function(e) {{
    var btn = e.target.closest('.criteria-sort-btn');
    if (!btn) return;
    e.stopPropagation();
    var sortKey = btn.getAttribute('data-sort-key');
    var container = btn.closest('.criteria-detail');
    if (!container) return;
    var bar = btn.closest('.criteria-sort-bar');
    var siblings = bar.querySelectorAll('.criteria-sort-btn');
    var wasAsc = btn.classList.contains('sort-asc');
    var wasDesc = btn.classList.contains('sort-desc');

    // Reset all buttons in this bar
    siblings.forEach(function(s) {{
      s.classList.remove('sort-asc', 'sort-desc');
      s.querySelector('.sort-arrow').textContent = '\u25B2';
    }});

    // Toggle: none -> asc -> desc -> none
    var dir;
    if (!wasAsc && !wasDesc) {{
      dir = 'asc';
    }} else if (wasAsc) {{
      dir = 'desc';
    }} else {{
      dir = 'none';
    }}

    if (dir !== 'none') {{
      btn.classList.add(dir === 'asc' ? 'sort-asc' : 'sort-desc');
      btn.querySelector('.sort-arrow').textContent = dir === 'asc' ? '\u25B2' : '\u25BC';
    }}

    var items = Array.prototype.slice.call(container.querySelectorAll('.criterion-item'));
    if (dir === 'none') {{
      // Restore original order by criterion ID
      items.sort(function(a, b) {{
        var idA = parseInt(a.querySelector('.criterion-id').textContent.replace('#', ''));
        var idB = parseInt(b.querySelector('.criterion-id').textContent.replace('#', ''));
        return idA - idB;
      }});
    }} else {{
      var attrName = 'data-sort-' + sortKey;
      var isNumeric = (sortKey === 'cost');
      items.sort(function(a, b) {{
        var vA = a.getAttribute(attrName) || '';
        var vB = b.getAttribute(attrName) || '';
        var cmp;
        if (isNumeric) {{
          cmp = (parseFloat(vA) || 0) - (parseFloat(vB) || 0);
        }} else {{
          cmp = vA.localeCompare(vB);
        }}
        return dir === 'asc' ? cmp : -cmp;
      }});
    }}

    // Re-insert items after the sort bar
    items.forEach(function(item) {{
      container.appendChild(item);
    }});
  }});

  // Sort headers
  headers.forEach(function(th) {{
    th.addEventListener('click', function() {{
      var col = parseInt(th.getAttribute('data-col'));
      if (sortCol === col) {{
        sortAsc = !sortAsc;
      }} else {{
        sortCol = col;
        sortAsc = true;
      }}
      headers.forEach(function(h) {{
        h.classList.remove('sort-asc', 'sort-desc');
        h.querySelector('.sort-arrow').textContent = '\u25B2';
      }});
      th.classList.add(sortAsc ? 'sort-asc' : 'sort-desc');
      th.querySelector('.sort-arrow').textContent = sortAsc ? '\u25B2' : '\u25BC';
      applySort();
    }});
  }});

  // Status filter chips
  chips.forEach(function(chip) {{
    chip.addEventListener('click', function() {{
      chips.forEach(function(c) {{ c.classList.remove('active'); }});
      chip.classList.add('active');
      statusFilter = chip.getAttribute('data-filter');
      applyFilter();
    }});
  }});

  // Search input
  searchInput.addEventListener('input', function() {{
    searchTerm = searchInput.value.toLowerCase();
    applyFilter();
  }});

  // Page size
  pageSizeEl.addEventListener('change', function() {{
    pageSize = parseInt(pageSizeEl.value);
    currentPage = 1;
    render();
  }});

  // Prev/Next
  prevBtn.addEventListener('click', function() {{
    if (currentPage > 1) {{ currentPage--; render(); }}
  }});
  nextBtn.addEventListener('click', function() {{
    var maxP = Math.ceil(filtered.length / pageSize);
    if (currentPage < maxP) {{ currentPage++; render(); }}
  }});

  // Initial render — sort by Last Updated descending
  applySort();
}})();
</script>

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
    # config_path accepted for dispatch consistency but unused currently
    # config_path = argv[1]
    log.debug("DB path: %s", db_path)

    if not os.path.isfile(db_path):
        print(f"Error: Database not found at {db_path}", file=sys.stderr)
        print("Run 'tusk init' first.", file=sys.stderr)
        sys.exit(1)

    # Fetch data
    conn = get_connection(db_path)
    task_metrics = fetch_task_metrics(conn)
    complexity_metrics = fetch_complexity_metrics(conn)
    cost_trend = fetch_cost_trend(conn)
    all_criteria = fetch_all_criteria(conn)
    conn.close()

    # Generate HTML
    html_content = generate_html(task_metrics, complexity_metrics, cost_trend, all_criteria)
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
