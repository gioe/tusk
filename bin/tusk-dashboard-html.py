"""HTML generation layer for the tusk dashboard.

Contains all HTML templating functions: formatters, component generators, and
section builders. Imported by tusk-dashboard.py via importlib.

Not a standalone CLI command — imported by tusk-dashboard.py via tusk_loader.
"""

import html
import json
import logging
import os
import sys
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-dashboard-css.py and tusk-dashboard-js.py

log = logging.getLogger(__name__)


# Expected session ranges per complexity tier (from CLAUDE.md)
EXPECTED_SESSIONS = {
    'XS': (0.5, 1),
    'S': (1, 1.5),
    'M': (1, 2),
    'L': (3, 5),
    'XL': (5, 10),
}

COMPLEXITY_SORT_ORDER = {'XS': 1, 'S': 2, 'M': 3, 'L': 4, 'XL': 5}


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


def format_status_duration(seconds) -> str:
    """Format seconds as time-in-status (e.g., '3d 4h', '2h 15m', '45m')."""
    if seconds is None or seconds == 0:
        return "0m"
    days = int(seconds) // 86400
    hours = (int(seconds) % 86400) // 3600
    minutes = (int(seconds) % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


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


def format_ctx_pct(pct, color: bool = False) -> str:
    """Format a context window percentage value, with optional color coding."""
    if pct is None:
        return '<span class="text-muted-dash">&mdash;</span>'
    pct_f = float(pct)
    label = f"{pct_f:.1f}%"
    if not color:
        return label
    if pct_f < 50:
        style = "color:#2d8a4e;font-weight:600"
    elif pct_f <= 80:
        style = "color:#b07d27;font-weight:600"
    else:
        style = "color:#c0392b;font-weight:600"
    return f'<span style="{style}">{label}</span>'


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


# ---------------------------------------------------------------------------
# DAG helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# HTML section generators
# ---------------------------------------------------------------------------

def generate_css() -> str:
    """Generate the full CSS wrapped in a <style> block."""
    return '<style>\n' + tusk_loader.load("tusk-dashboard-css").CSS + '\n</style>'


def generate_header(now: str, tz_label: str = "", project_name: str = "Tusk") -> str:
    """Generate the page header bar with theme toggle and tab navigation."""
    tz_suffix = f" ({esc(tz_label)})" if tz_label else ""
    return f"""\
<div class="header">
  <h1>{esc(project_name)} &mdash; Task Metrics</h1>
  <div style="display:flex;align-items:center;gap:var(--sp-3);">
    <span class="timestamp">Generated {esc(now)}{tz_suffix}</span>
    <button class="theme-toggle" id="themeToggle" title="Toggle dark mode" aria-label="Toggle dark mode">
      <span class="icon-sun">\u2600\uFE0F</span>
      <span class="icon-moon">\U0001F319</span>
    </button>
  </div>
</div>
<div class="tab-bar" id="tabBar">
  <button class="tab-btn active" data-tab="dashboard">Tasks</button>
  <button class="tab-btn" data-tab="dag">DAG</button>
  <button class="tab-btn" data-tab="skills">Skills</button>
  <button class="tab-btn" data-tab="cost">Cost</button>
  <button class="tab-btn" data-tab="models">Models</button>
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


def generate_skill_run_costs_section(skill_runs: list[dict]) -> str:
    """Generate the Skill Run Costs KPI cards panel for the Cost tab."""
    if not skill_runs:
        return """\
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header">Skill Run Costs</div>
  <p class="empty" style="padding: var(--sp-4);">No skill runs recorded yet.</p>
</div>"""

    skill_totals: dict[str, float] = defaultdict(float)
    for r in skill_runs:
        skill_totals[r['skill_name']] += r.get('cost_dollars') or 0

    total_runs = len(skill_runs)
    total_cost = sum(r.get('cost_dollars') or 0 for r in skill_runs)
    avg_cost = total_cost / total_runs if total_runs else 0
    most_expensive_skill = max(skill_totals, key=lambda k: skill_totals[k]) if skill_totals else "\u2014"

    return f"""\
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header">Skill Run Costs</div>
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
  </div>
</div>"""


def generate_skill_runs_section(skill_runs: list[dict], tool_stats_by_run: dict = None) -> str:
    """Generate the All Runs table panel for the Skills tab."""
    if tool_stats_by_run is None:
        tool_stats_by_run = {}
    if not skill_runs:
        return """\
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header">All Runs</div>
  <p class="empty" style="padding: var(--sp-4);">No skill runs recorded yet.</p>
</div>"""

    total_runs = len(skill_runs)
    top3_ids = (
        {r['id'] for r in sorted(skill_runs, key=lambda x: x.get('cost_dollars') or 0, reverse=True)[:3]}
        if total_runs > 3 else set()
    )

    all_costs = [r.get('cost_dollars') or 0 for r in skill_runs]
    max_cost = max(all_costs) if all_costs else 0

    def cost_cell_style(cost: float) -> str:
        if max_cost <= 0 or cost <= 0:
            return "text-align:right;font-variant-numeric:tabular-nums;"
        ratio = cost / max_cost
        if ratio >= 0.8:
            bg = "background-color:#fecaca;color:#7f1d1d;"
        elif ratio >= 0.5:
            bg = "background-color:#fed7aa;color:#7c2d12;"
        elif ratio >= 0.2:
            bg = "background-color:#dcfce7;color:#14532d;"
        else:
            bg = ""
        return f"text-align:right;font-variant-numeric:tabular-nums;{bg}"

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

    return f"""\
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header">All Runs</div>
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


def generate_cost_trend_section(cost_trend: list[dict], cost_trend_daily: list[dict],
                                cost_trend_monthly: list[dict], skill_runs: list[dict] = None) -> str:
    """Generate Cost Trend panel with period toggle and separate Task/Skill charts."""
    skill_runs = skill_runs or []

    # --- Task chart data ---
    daily_data = _build_chart_dataset(cost_trend_daily, "day", "daily_cost", "Daily")
    weekly_data = _build_chart_dataset(cost_trend, "week_start", "weekly_cost", "Weekly")
    monthly_data = _build_chart_dataset(cost_trend_monthly, "month", "monthly_cost", "Monthly")
    chart_data = json.dumps({
        "daily": daily_data,
        "weekly": weekly_data,
        "monthly": monthly_data,
    }).replace("</", "<\\/")
    has_cost_data = any(d["costs"] for d in [daily_data, weekly_data, monthly_data])
    empty_msg = '<p class="empty" style="padding:var(--sp-4) 0;">No session cost data available yet.</p>' if not has_cost_data else ''

    # --- Skill trend data (aggregated by day/week/month) ---
    skill_daily_agg: dict[str, float] = {}
    skill_weekly_agg: dict[str, float] = {}
    skill_monthly_agg: dict[str, float] = {}
    for r in skill_runs:
        cost = r.get('cost_dollars') or 0
        if not cost:
            continue
        started = _parse_dt(r.get('started_at') or '')
        if started is None:
            continue
        local = started.astimezone()
        day_key = local.strftime('%Y-%m-%d')
        week_start = (local - timedelta(days=local.weekday())).strftime('%Y-%m-%d')
        month_key = local.strftime('%Y-%m')
        skill_daily_agg[day_key] = round(skill_daily_agg.get(day_key, 0) + cost, 4)
        skill_weekly_agg[week_start] = round(skill_weekly_agg.get(week_start, 0) + cost, 4)
        skill_monthly_agg[month_key] = round(skill_monthly_agg.get(month_key, 0) + cost, 4)

    skill_daily_rows = [{"day": k, "daily_cost": v} for k, v in sorted(skill_daily_agg.items())]
    skill_weekly_rows = [{"week_start": k, "weekly_cost": v} for k, v in sorted(skill_weekly_agg.items())]
    skill_monthly_rows = [{"month": k, "monthly_cost": v} for k, v in sorted(skill_monthly_agg.items())]
    skill_trend_data = json.dumps({
        "daily": _build_chart_dataset(skill_daily_rows, "day", "daily_cost", "Daily"),
        "weekly": _build_chart_dataset(skill_weekly_rows, "week_start", "weekly_cost", "Weekly"),
        "monthly": _build_chart_dataset(skill_monthly_rows, "month", "monthly_cost", "Monthly"),
    }).replace("</", "<\\/")
    has_skill_trend = bool(skill_daily_agg or skill_weekly_agg or skill_monthly_agg)
    empty_skill_msg = '<p class="empty" style="padding:var(--sp-4) 0;">No skill cost data available yet.</p>' if not has_skill_trend else ''

    task_chart_hidden = ' display:none;' if not has_cost_data else ''
    skill_chart_hidden = ' display:none;' if not has_skill_trend else ''

    return f"""\
<script>
window.__tuskCostTrend = {chart_data};
window.__tuskSkillTrend = {skill_trend_data};
</script>
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header" style="display:flex;align-items:center;justify-content:space-between;">
    <span>Cost Trend</span>
    <div class="cost-trend-controls">
      <span class="cost-toggle-label">Source</span>
      <div class="cost-trend-tabs" id="costTypeTabs">
        <button class="cost-tab active" data-type="task">Tasks</button>
        <button class="cost-tab" data-type="skill">Skills</button>
      </div>
      <span class="cost-controls-sep"></span>
      <span class="cost-toggle-label">Period</span>
      <div class="cost-trend-tabs" id="costTrendTabs">
        <button class="cost-tab" data-tab="daily">Daily</button>
        <button class="cost-tab active" data-tab="weekly">Weekly</button>
        <button class="cost-tab" data-tab="monthly">Monthly</button>
      </div>
    </div>
  </div>
  <div id="costTaskView" style="padding:0 var(--sp-4) var(--sp-4);">
    {empty_msg}
    <canvas id="costTrendChart" height="220" style="max-width:100%;width:100%;{task_chart_hidden}"></canvas>
  </div>
  <div id="costSkillView" style="display:none;padding:0 var(--sp-4) var(--sp-4);">
    {empty_skill_msg}
    <canvas id="costSkillTrendChart" height="220" style="max-width:100%;width:100%;{skill_chart_hidden}"></canvas>
  </div>
</div>
<script>
(function() {{
  var typeBtns = document.querySelectorAll('#costTypeTabs .cost-tab');
  var taskView = document.getElementById('costTaskView');
  var skillView = document.getElementById('costSkillView');
  typeBtns.forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var type = btn.getAttribute('data-type');
      typeBtns.forEach(function(b) {{ b.classList.remove('active'); }});
      btn.classList.add('active');
      if (type === 'skill') {{
        taskView.style.display = 'none';
        skillView.style.display = '';
      }} else {{
        taskView.style.display = '';
        skillView.style.display = 'none';
      }}
    }});
  }});
}})();
</script>"""


def generate_cost_per_user_prompt_section(weekly_rows: list[dict]) -> str:
    """Generate the Cost Per User Prompt weekly trend panel.

    weekly_rows: list of {week_start, weekly_cost, weekly_prompts, cost_per_prompt}
    from fetch_cost_per_user_prompt_trend. Tracks user prompting efficiency
    over time — flat or rising means costlier prompting, falling means
    more efficient.
    """
    rows = weekly_rows or []
    labels = [r["week_start"] for r in rows]
    values = [r.get("cost_per_prompt") or 0 for r in rows]
    prompts = [r.get("weekly_prompts") or 0 for r in rows]
    payload = json.dumps({
        "labels": labels,
        "values": values,
        "prompts": prompts,
    }).replace("</", "<\\/")

    has_data = any(values)
    empty_msg = (
        '<p class="empty" style="padding:var(--sp-4) 0;">'
        'No skill runs with user-prompt data yet — the metric needs at least '
        'one finished /tusk run after the v60 migration.'
        '</p>'
        if not has_data else ''
    )
    chart_hidden = ' display:none;' if not has_data else ''

    return f"""\
<script>window.__tuskCostPerUserPrompt = {payload};</script>
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header">Cost Per User Prompt (Weekly)</div>
  <p class="muted" style="padding:0 var(--sp-4) var(--sp-3);font-size:0.85em;">
    Weekly average dollars per user prompt across all skill runs.
    Optimize this — not raw token count. A clear-but-verbose prompt that prevents
    three rounds of clarification beats a cryptic one-liner that triggers iteration.
  </p>
  <div style="padding:0 var(--sp-4) var(--sp-4);">
    {empty_msg}
    <canvas id="costPerUserPromptChart" height="220" style="max-width:100%;width:100%;{chart_hidden}"></canvas>
  </div>
</div>
<script>
(function() {{
  if (typeof Chart === 'undefined') return;
  var data = window.__tuskCostPerUserPrompt;
  if (!data || !data.values || !data.values.length) return;
  var canvas = document.getElementById('costPerUserPromptChart');
  if (!canvas) return;
  var style = getComputedStyle(document.documentElement);
  var textMuted = style.getPropertyValue('--text-muted').trim() || '#94a3b8';
  var border = style.getPropertyValue('--border').trim() || '#e2e8f0';
  var accent = style.getPropertyValue('--accent').trim() || '#3b82f6';
  new Chart(canvas, {{
    type: 'line',
    data: {{
      labels: data.labels,
      datasets: [{{
        label: 'Cost / User Prompt',
        data: data.values,
        borderColor: accent,
        backgroundColor: accent + '33',
        tension: 0.25,
        pointRadius: 4,
        pointHoverRadius: 6,
        fill: true
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ labels: {{ color: textMuted, usePointStyle: true, padding: 16 }} }},
        tooltip: {{
          callbacks: {{
            label: function(ctx) {{
              var v = ctx.parsed.y || 0;
              var n = (data.prompts && data.prompts[ctx.dataIndex]) || 0;
              return '$' + v.toFixed(4) + ' / prompt (' + n + ' prompts)';
            }}
          }}
        }}
      }},
      scales: {{
        x: {{ ticks: {{ color: textMuted, font: {{ size: 11 }}, maxTicksLimit: 12 }}, grid: {{ color: border, borderDash: [3,3] }} }},
        y: {{
          title: {{ display: true, text: 'Cost / Prompt ($)', color: textMuted }},
          ticks: {{ color: textMuted, font: {{ size: 11 }}, callback: function(v) {{ return '$' + v.toFixed(4); }} }},
          grid: {{ color: border, borderDash: [3,3] }},
          beginAtZero: true
        }}
      }}
    }}
  }});
}})();
</script>"""


def generate_hourly_cost_section() -> str:
    """Generate Hour of Day Cost panel with Tasks vs Skills toggle."""
    return """\
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header" style="display:flex;align-items:center;justify-content:space-between;">
    <span>Hour of Day Cost</span>
    <div class="cost-trend-controls">
      <span class="cost-toggle-label">Source</span>
      <div class="cost-trend-tabs" id="hourlyTypeTabs">
        <button class="cost-tab active" data-type="task">Tasks</button>
        <button class="cost-tab" data-type="skill">Skills</button>
      </div>
    </div>
  </div>
  <div id="hourlyTaskView" style="padding:0 var(--sp-4) var(--sp-4);">
    <canvas id="hourlyCostTaskChart" height="200" style="max-width:100%;width:100%;"></canvas>
  </div>
  <div id="hourlySkillView" style="display:none;padding:0 var(--sp-4) var(--sp-4);">
    <canvas id="hourlyCostSkillChart" height="200" style="max-width:100%;width:100%;"></canvas>
  </div>
</div>
<script>
(function() {{
  var typeBtns = document.querySelectorAll('#hourlyTypeTabs .cost-tab');
  var taskView = document.getElementById('hourlyTaskView');
  var skillView = document.getElementById('hourlySkillView');
  typeBtns.forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var type = btn.getAttribute('data-type');
      typeBtns.forEach(function(b) {{ b.classList.remove('active'); }});
      btn.classList.add('active');
      if (type === 'skill') {{
        taskView.style.display = 'none';
        skillView.style.display = '';
      }} else {{
        taskView.style.display = '';
        skillView.style.display = 'none';
      }}
    }});
  }});
}})();
</script>"""


def generate_cost_scatter_section() -> str:
    """Generate Cost by Model scatter plot panel with X-axis toggle."""
    return """\
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header" style="display:flex;align-items:center;justify-content:space-between;">
    <span>Cost by Model</span>
    <div class="cost-trend-controls">
      <span class="cost-toggle-label">X-Axis</span>
      <div class="cost-trend-tabs" id="scatterXTabs">
        <button class="cost-tab active" data-axis="tokens">Tokens</button>
        <button class="cost-tab" data-axis="date">Date</button>
        <button class="cost-tab" data-axis="duration">Duration</button>
      </div>
    </div>
  </div>
  <div style="padding:0 var(--sp-4) var(--sp-4);">
    <canvas id="costScatterChart" height="280" style="max-width:100%;width:100%;"></canvas>
  </div>
  <p style="padding:0 var(--sp-4) var(--sp-4);font-size:0.7rem;color:var(--text-muted);margin:0;">
    Each point is a session. Hover for details. Color = model.
  </p>
</div>
<script>
(function() {
  var axisBtns = document.querySelectorAll('#scatterXTabs .cost-tab');
  axisBtns.forEach(function(btn) {
    btn.addEventListener('click', function() {
      axisBtns.forEach(function(b) { b.classList.remove('active'); });
      btn.classList.add('active');
      if (window.__tuskRebuildScatter) {
        window.__tuskRebuildScatter(btn.getAttribute('data-axis'));
      }
    });
  });
})();
</script>"""


def generate_models_section(model_performance: dict | None) -> str:
    """Generate the Models tab body: KPI cards with a Tasks/Skills/Both source toggle."""
    data = model_performance or {}
    payload = {
        "models": data.get("models") or [],
        "complexity_matrix": data.get("complexity_matrix") or [],
        "timeseries_tasks": data.get("timeseries_tasks") or [],
        "timeseries_skills": data.get("timeseries_skills") or [],
    }

    has_resolved_signal = False
    for m in payload["models"]:
        name = (m.get("model") or "").strip()
        if not name or name == "unknown":
            continue
        if (m.get("task_session_count") or 0) or (m.get("skill_run_count") or 0):
            has_resolved_signal = True
            break
    if not has_resolved_signal:
        return """\
<div class="panel">
  <div class="section-header"><span>Models</span></div>
  <div style="padding: var(--sp-4);">
    <p class="empty">No model data yet &mdash; close a session to populate.</p>
  </div>
</div>
"""

    payload_json = json.dumps(payload).replace("</", "<\\/")

    panel_html = """\
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header" style="display:flex;align-items:center;justify-content:space-between;">
    <span>Models</span>
    <div class="cost-trend-controls">
      <span class="cost-toggle-label">Source</span>
      <div class="cost-trend-tabs" id="modelsSourceTabs">
        <button class="cost-tab active" data-source="both">Both</button>
        <button class="cost-tab" data-source="tasks">Tasks</button>
        <button class="cost-tab" data-source="skills">Skills</button>
      </div>
    </div>
  </div>
  <div id="modelsKpiGrid" class="kpi-grid" style="padding: 0 var(--sp-4) var(--sp-4);"></div>
</div>
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header"><span>Avg Turns &amp; Cost by Complexity</span></div>
  <div style="padding: 0 var(--sp-4) var(--sp-4);overflow-x:auto;">
    <table id="modelsComplexityTable">
      <thead>
        <tr>
          <th>Model</th>
          <th style="text-align:right">XS</th>
          <th style="text-align:right">S</th>
          <th style="text-align:right">M</th>
          <th style="text-align:right">L</th>
          <th style="text-align:right">XL</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
    <p style="font-size:0.7rem;color:var(--text-muted);margin:var(--sp-2) 0 0;">
      Each cell shows <span style="white-space:nowrap">avg&nbsp;turns</span> / <span style="white-space:nowrap">avg&nbsp;cost</span> per session in that complexity bucket. Derived from task sessions only.
    </p>
  </div>
</div>
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header" style="display:flex;align-items:center;justify-content:space-between;">
    <span>Trend</span>
    <div class="cost-trend-controls">
      <span class="cost-toggle-label">Y-Axis</span>
      <div class="cost-trend-tabs" id="modelsMetricTabs">
        <button class="cost-tab active" data-metric="cost">Cost</button>
        <button class="cost-tab" data-metric="cost_per_loc">Cost / LOC</button>
        <button class="cost-tab" data-metric="turns">Turns</button>
        <button class="cost-tab" data-metric="tokens">Tokens</button>
      </div>
    </div>
  </div>
  <div style="padding: 0 var(--sp-4) var(--sp-4);">
    <canvas id="modelsTrendChart" height="280" style="max-width:100%;width:100%;"></canvas>
  </div>
  <p style="padding:0 var(--sp-4) var(--sp-4);font-size:0.7rem;color:var(--text-muted);margin:0;">
    One line per model. Cost/LOC uses task lines added + removed on the same day (skill runs contribute 0 LOC).
  </p>
</div>
"""

    script = """\
<script>
(function() {
  var data = window.__tuskModels || {};
  var models = data.models || [];
  var complexityMatrix = data.complexity_matrix || [];
  var tsTasks = data.timeseries_tasks || [];
  var tsSkills = data.timeseries_skills || [];

  var source = 'both';
  var metric = 'cost';
  var COMPLEXITY_TIERS = ['XS', 'S', 'M', 'L', 'XL'];
  var PALETTE = ['#3b82f6', '#f59e0b', '#10b981', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4', '#f97316'];
  var trendChart = null;

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fmtCost(v) { return '$' + (v || 0).toFixed(2); }
  function fmtCostFine(v) { return '$' + (v || 0).toFixed(4); }
  function fmtTokens(n) {
    n = n || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(n|0);
  }
  function fmtInt(n) { return (n || 0).toLocaleString(); }

  function summarize(m) {
    var cost = 0, tokensIn = 0, tokensOut = 0, reqs = 0, reqsKnown = false, sessions = 0, tasks = 0, linesAdd = 0, linesRem = 0, skillRuns = 0;
    if (source === 'tasks' || source === 'both') {
      cost += m.task_cost || 0;
      tokensIn += m.task_tokens_in || 0;
      tokensOut += m.task_tokens_out || 0;
      if (m.task_request_count != null) { reqs += m.task_request_count; reqsKnown = true; }
      sessions += m.task_session_count || 0;
      tasks += m.task_count || 0;
      linesAdd += m.task_lines_added || 0;
      linesRem += m.task_lines_removed || 0;
    }
    if (source === 'skills' || source === 'both') {
      cost += m.skill_cost || 0;
      tokensIn += m.skill_tokens_in || 0;
      tokensOut += m.skill_tokens_out || 0;
      if (m.skill_request_count != null) { reqs += m.skill_request_count; reqsKnown = true; }
      skillRuns += m.skill_run_count || 0;
    }
    var loc = linesAdd + linesRem;
    return {
      model: m.model, cost: cost, tokens_in: tokensIn, tokens_out: tokensOut,
      requests: reqsKnown ? reqs : null, requests_known: reqsKnown,
      sessions: sessions, skill_runs: skillRuns, tasks: tasks,
      loc: loc,
      cost_per_task: tasks > 0 ? cost / tasks : 0,
      cost_per_loc: loc > 0 ? cost / loc : 0,
      cost_per_turn: (reqsKnown && reqs > 0) ? cost / reqs : 0
    };
  }

  function renderKpi() {
    var host = document.getElementById('modelsKpiGrid');
    if (!host) return;
    var rows = models.map(summarize).filter(function(s) {
      return s.cost > 0 || s.sessions > 0 || s.skill_runs > 0 || (s.requests || 0) > 0;
    });
    rows.sort(function(a, b) { return b.cost - a.cost; });
    if (!rows.length) {
      host.innerHTML = '<p class="empty" style="grid-column:1/-1;">No data for this source.</p>';
      return;
    }
    host.innerHTML = rows.map(function(r) {
      var srcDetail;
      if (source === 'tasks') {
        srcDetail = fmtInt(r.sessions) + ' session' + (r.sessions === 1 ? '' : 's');
      } else if (source === 'skills') {
        srcDetail = fmtInt(r.skill_runs) + ' skill run' + (r.skill_runs === 1 ? '' : 's');
      } else {
        srcDetail = fmtInt(r.sessions) + ' / ' + fmtInt(r.skill_runs) + ' (sess/skill)';
      }
      var costPerLoc = r.loc > 0 ? '<div class="kpi-sub">' + fmtCostFine(r.cost_per_loc) + ' / LOC</div>' : '';
      var costPerTask = (source !== 'skills' && r.tasks > 0) ? '<div class="kpi-sub">' + fmtCost(r.cost_per_task) + ' / task</div>' : '';
      var turnsText = r.requests_known ? fmtInt(r.requests) + ' turns' : '\u2014 turns';
      return (
        '<div class="kpi-card">' +
          '<div class="kpi-label">' + escapeHtml(r.model) + '</div>' +
          '<div class="kpi-value">' + fmtCost(r.cost) + '</div>' +
          '<div class="kpi-sub">' + srcDetail + '</div>' +
          '<div class="kpi-sub">' + turnsText + ' \u00b7 ' + fmtTokens(r.tokens_in + r.tokens_out) + ' tok</div>' +
          costPerTask +
          costPerLoc +
        '</div>'
      );
    }).join('');
  }

  function renderComplexityTable() {
    var table = document.getElementById('modelsComplexityTable');
    if (!table) return;
    var tbody = table.querySelector('tbody');
    tbody.innerHTML = '';
    if (source === 'skills') {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">Complexity breakdown is task-session only.</td></tr>';
      return;
    }
    var byModel = {};
    complexityMatrix.forEach(function(row) {
      if (!byModel[row.model]) byModel[row.model] = {};
      byModel[row.model][row.complexity] = row;
    });
    var modelNames = Object.keys(byModel).sort();
    if (!modelNames.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">No tasks with resolved complexity yet.</td></tr>';
      return;
    }
    var out = '';
    modelNames.forEach(function(name) {
      out += '<tr><td>' + escapeHtml(name) + '</td>';
      COMPLEXITY_TIERS.forEach(function(tier) {
        var cell = byModel[name][tier];
        if (!cell) {
          out += '<td class="text-muted-dash" style="text-align:right">\u2014</td>';
        } else {
          var turns = cell.avg_turns == null ? '\u2014' : Number(cell.avg_turns).toFixed(1);
          out += '<td style="text-align:right">'
            + turns
            + ' / ' + fmtCostFine(cell.avg_cost)
            + '</td>';
        }
      });
      out += '</tr>';
    });
    tbody.innerHTML = out;
  }

  function combinedTimeseries() {
    if (source === 'tasks') return tsTasks.slice();
    if (source === 'skills') return tsSkills.slice();
    var merged = {};
    function add(arr) {
      arr.forEach(function(r) {
        var k = r.day + '|' + r.model;
        if (!merged[k]) merged[k] = { day: r.day, model: r.model, cost: 0, request_count: null, total_lines: 0, total_tokens: 0 };
        merged[k].cost += r.cost || 0;
        if (r.request_count != null) {
          merged[k].request_count = (merged[k].request_count || 0) + r.request_count;
        }
        merged[k].total_lines += r.total_lines || 0;
        merged[k].total_tokens += r.total_tokens || 0;
      });
    }
    add(tsTasks);
    add(tsSkills);
    return Object.keys(merged).map(function(k) { return merged[k]; });
  }

  function buildTrendConfig() {
    var src = combinedTimeseries();
    var daySet = {}, modelSet = {};
    src.forEach(function(r) { daySet[r.day] = true; modelSet[r.model] = true; });
    var days = Object.keys(daySet).sort();
    var modelNames = Object.keys(modelSet).sort();
    var lookup = {};
    src.forEach(function(r) { lookup[r.day + '|' + r.model] = r; });
    var datasets = modelNames.map(function(name, i) {
      var color = PALETTE[i % PALETTE.length];
      var points = days.map(function(day) {
        var r = lookup[day + '|' + name];
        if (!r) return 0;
        if (metric === 'cost') return r.cost || 0;
        if (metric === 'turns') return r.request_count == null ? null : r.request_count;
        if (metric === 'tokens') return r.total_tokens || 0;
        if (metric === 'cost_per_loc') return (r.total_lines || 0) > 0 ? (r.cost || 0) / r.total_lines : 0;
        return 0;
      });
      return {
        label: name,
        data: points,
        borderColor: color,
        backgroundColor: color + '33',
        tension: 0.25,
        pointRadius: 3,
        pointHoverRadius: 6,
        fill: false
      };
    });
    return { labels: days, datasets: datasets };
  }

  function renderChart() {
    var canvas = document.getElementById('modelsTrendChart');
    if (!canvas || typeof Chart === 'undefined') return;
    if (trendChart) { trendChart.destroy(); trendChart = null; }
    var cfg = buildTrendConfig();
    var style = getComputedStyle(document.documentElement);
    var textMuted = style.getPropertyValue('--text-muted').trim() || '#94a3b8';
    var border = style.getPropertyValue('--border').trim() || '#e2e8f0';
    var yTitle;
    if (metric === 'cost') yTitle = 'Cost ($)';
    else if (metric === 'cost_per_loc') yTitle = 'Cost / LOC ($)';
    else if (metric === 'turns') yTitle = 'Turns';
    else yTitle = 'Tokens';
    trendChart = new Chart(canvas, {
      type: 'line',
      data: cfg,
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: textMuted, usePointStyle: true, padding: 16 } },
          tooltip: {
            callbacks: {
              label: function(ctx) {
                var v = ctx.parsed.y || 0;
                if (metric === 'cost') return ctx.dataset.label + ': $' + v.toFixed(4);
                if (metric === 'cost_per_loc') return ctx.dataset.label + ': $' + v.toFixed(6) + ' / LOC';
                if (metric === 'turns') return ctx.dataset.label + ': ' + Math.round(v) + ' turns';
                return ctx.dataset.label + ': ' + v.toLocaleString() + ' tokens';
              }
            }
          }
        },
        scales: {
          x: { ticks: { color: textMuted, font: { size: 11 }, maxTicksLimit: 12 }, grid: { color: border, borderDash: [3,3] } },
          y: {
            title: { display: true, text: yTitle, color: textMuted },
            ticks: {
              color: textMuted,
              font: { size: 11 },
              callback: function(v) {
                if (metric === 'cost') return '$' + v.toFixed(2);
                if (metric === 'cost_per_loc') return '$' + v.toFixed(4);
                if (metric === 'tokens') return v >= 1e6 ? (v/1e6).toFixed(1) + 'M' : v >= 1e3 ? (v/1e3).toFixed(0) + 'K' : v;
                return v;
              }
            },
            grid: { color: border, borderDash: [3,3] },
            beginAtZero: true
          }
        }
      }
    });
  }

  function renderAll() {
    renderKpi();
    renderComplexityTable();
    renderChart();
  }

  var srcBtns = document.querySelectorAll('#modelsSourceTabs .cost-tab');
  srcBtns.forEach(function(btn) {
    btn.addEventListener('click', function() {
      srcBtns.forEach(function(b) { b.classList.remove('active'); });
      btn.classList.add('active');
      source = btn.getAttribute('data-source');
      renderAll();
    });
  });

  var metricBtns = document.querySelectorAll('#modelsMetricTabs .cost-tab');
  metricBtns.forEach(function(btn) {
    btn.addEventListener('click', function() {
      metricBtns.forEach(function(b) { b.classList.remove('active'); });
      btn.classList.add('active');
      metric = btn.getAttribute('data-metric');
      renderChart();
    });
  });

  renderAll();
})();
</script>
"""

    return f'<script>window.__tuskModels = {payload_json};</script>\n' + panel_html + script


def generate_rework_rate_section(
    rework_rate: list[dict] | None, min_sample_size: int = 5
) -> str:
    """Per-model rework rate panel for the Models tab.

    Renders one row per closer-model with shipped / rework / rate columns.
    Models with fewer than `min_sample_size` shipped tasks keep their raw
    counts but show '—' for the rate so an early noisy ratio doesn't dominate.
    """
    rows = rework_rate or []
    if not rows:
        return """\
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header"><span>Rework Rate</span></div>
  <div style="padding: var(--sp-4);">
    <p class="empty">No shipped feature/bug tasks with closer sessions yet.</p>
  </div>
</div>
"""

    body = ""
    for r in rows:
        shipped = r.get("shipped_tasks") or 0
        rework = r.get("rework_tasks") or 0
        rate = r.get("rework_rate")
        meets = bool(r.get("meets_threshold"))
        if meets and rate is not None:
            rate_cell = f"{rate * 100:.1f}%"
        else:
            rate_cell = '<span class="text-muted-dash">\u2014</span>'
        body += (
            "<tr>"
            f"<td>{esc(r.get('model') or 'unknown')}</td>"
            f'<td style="text-align:right">{shipped}</td>'
            f'<td style="text-align:right">{rework}</td>'
            f'<td style="text-align:right">{rate_cell}</td>'
            "</tr>"
        )

    return f"""\
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header"><span>Rework Rate</span></div>
  <div style="padding: 0 var(--sp-4) var(--sp-4);overflow-x:auto;">
    <table>
      <thead>
        <tr>
          <th>Model</th>
          <th style="text-align:right">Shipped</th>
          <th style="text-align:right">Rework</th>
          <th style="text-align:right">Rate</th>
        </tr>
      </thead>
      <tbody>
        {body}
      </tbody>
    </table>
    <p style="font-size:0.7rem;color:var(--text-muted);margin:var(--sp-2) 0 0;">
      Shipped = completed feature/bug tasks attributed to the model of the most
      recently ended session. Rework = follow-up tasks filed against them via
      <code>tasks.fixes_task_id</code>. Rate is hidden (&mdash;) until a model has at
      least {min_sample_size} shipped tasks. Lower is better.
    </p>
  </div>
</div>
"""


def generate_dow_hour_heatmap_section() -> str:
    """Generate Day-of-Week × Hour heatmap panel (rendered by JS)."""
    return """\
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header">
    <span>Activity by Day &amp; Hour</span>
  </div>
  <div id="dowHourHeatmapContainer" style="padding:0 var(--sp-4) var(--sp-2);"></div>
  <p style="padding:0 var(--sp-4) var(--sp-4);font-size:0.7rem;color:var(--text-muted);margin:0;">
    Day and hour labels are local time.
  </p>
</div>"""


def generate_filter_bar() -> str:
    """Generate the filter chips, dropdowns, search input, and filter badge."""
    return """\
<div class="filter-bar">
  <select class="filter-select" id="statusFilter">
    <option value="All">Status</option>
    <option value="To Do">To Do</option>
    <option value="In Progress">In Progress</option>
    <option value="Done">Done</option>
  </select>
  <select class="filter-select" id="complexityFilter"><option value="">Size</option></select>
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
    <th data-col="2" data-type="num" style="text-align:right" class="sort-desc">Cost <span class="sort-arrow">\u25BC</span></th>
    <th data-col="3" data-type="str">Status <span class="sort-arrow">\u25B2</span></th>
    <th data-col="4" data-type="num" style="text-align:right" title="For Done tasks: wall-clock span from first session start to last session end (includes gaps between sessions). For active tasks: time elapsed since first started (In Progress) or created (To Do).">Duration <span class="sort-arrow">\u25B2</span></th>
    <th data-col="5" data-type="num">Size <span class="sort-arrow">\u25B2</span></th>
    <th data-col="6" data-type="num" style="text-align:right">WSJF <span class="sort-arrow">\u25B2</span></th>
    <th data-col="7" data-type="str">Model <span class="sort-arrow">\u25B2</span></th>
    <th data-col="8" data-type="num" style="text-align:right">Work Time <span class="sort-arrow">\u25B2</span></th>
    <th data-col="9" data-type="num" style="text-align:right">Lines <span class="sort-arrow">\u25B2</span></th>
    <th data-col="10" data-type="num" style="text-align:right">Tokens In <span class="sort-arrow">\u25B2</span></th>
    <th data-col="11" data-type="num" style="text-align:right">Tokens Out <span class="sort-arrow">\u25B2</span></th>
    <th data-col="12" data-type="num" style="text-align:right" title="Context window % at the start of the earliest session">Ctx% Start <span class="sort-arrow">\u25B2</span></th>
    <th data-col="13" data-type="num" style="text-align:right" title="Peak context window % reached across all sessions">Ctx% Peak <span class="sort-arrow">\u25B2</span></th>
    <th data-col="14" data-type="num" style="text-align:right" title="Context window % at the end of the latest session">Ctx% End <span class="sort-arrow">\u25B2</span></th>
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


def generate_criteria_detail(tid: int, has_criteria: bool = True, tool_stats: list[dict] = None) -> str:
    """Generate the collapsible detail row for a task.

    Contains an optional criteria panel (client-side rendered from JSON) and
    an optional tool cost breakdown panel (server-side rendered).
    """
    inner = ""

    if has_criteria:
        sort_bar = (
            '<div class="criteria-sort-bar">'
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
        f'  <td colspan="15">{inner}</td>\n'
        f'</tr>\n'
    )


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
    status_duration_seconds = t.get('duration_in_status_seconds') or 0
    lines_added = t.get('total_lines_added') or 0
    lines_removed = t.get('total_lines_removed') or 0
    total_lines = int(lines_added) + int(lines_removed)
    dep_badges = build_dep_badges(tid, task_deps, summary_map)
    summary_cell = f'<div class="summary-text">{esc(t["summary"])}</div>{dep_badges}'

    # Cost heatmap class for the cost cell
    heat_cls = cost_heat_class(t['total_cost'], max_cost)
    cost_cls = f'col-cost {heat_cls}'.strip()

    row = f"""<tr{cls_attr} data-status="{status_val}" data-summary="{esc(t['summary']).lower()}" data-task-id="{tid}" data-complexity="{complexity_val}" data-type="{task_type_val}">
  <td class="col-id" data-sort="{tid}">{toggle_icon}#{tid}</td>
  <td class="col-summary">{summary_cell}</td>
  <td class="{cost_cls}" data-sort="{t['total_cost']}">{format_cost(t['total_cost'])}</td>
  <td class="col-status"><span class="status-badge status-{status_val.lower().replace(' ', '-')}">{status_val}</span></td>
  <td class="col-status-duration" data-sort="{status_duration_seconds}" style="text-align:right">{format_status_duration(status_duration_seconds) if status_duration_seconds else '<span class="text-muted-dash">&mdash;</span>'}</td>
  <td class="col-complexity" data-sort="{complexity_sort}">{f'<span class="complexity-badge">{complexity_val}</span>' if complexity_val else ''}</td>
  <td class="col-wsjf" data-sort="{priority_score}">{priority_score}</td>
  <td class="col-model" data-sort="{esc(models_raw)}" title="{esc(models_raw)}">{esc(models_raw) if models_raw else '<span class="text-muted-dash">&mdash;</span>'}</td>
  <td class="col-duration" data-sort="{duration_seconds}">{format_duration(duration_seconds) if duration_seconds else '<span class="text-muted-dash">&mdash;</span>'}</td>
  <td class="col-lines" data-sort="{total_lines}" data-lines-added="{int(lines_added)}" data-lines-removed="{int(lines_removed)}">{format_lines_html(lines_added, lines_removed)}</td>
  <td class="col-tokens-in" data-sort="{t['total_tokens_in']}">{format_tokens_compact(t['total_tokens_in'])}</td>
  <td class="col-tokens-out" data-sort="{t['total_tokens_out']}">{format_tokens_compact(t['total_tokens_out'])}</td>
  <td class="col-ctx-start" data-sort="{t.get('first_ctx_pct') if t.get('first_ctx_pct') is not None else -1}" style="text-align:right">{format_ctx_pct(t.get('first_ctx_pct'))}</td>
  <td class="col-ctx-peak" data-sort="{t.get('peak_ctx_pct') if t.get('peak_ctx_pct') is not None else -1}" style="text-align:right">{format_ctx_pct(t.get('peak_ctx_pct'), color=True)}</td>
  <td class="col-ctx-end" data-sort="{t.get('last_ctx_pct') if t.get('last_ctx_pct') is not None else -1}" style="text-align:right">{format_ctx_pct(t.get('last_ctx_pct'))}</td>
</tr>\n"""

    if has_expandable:
        row += generate_criteria_detail(tid, has_criteria=has_criteria, tool_stats=tool_stats)

    return row



def generate_pagination() -> str:
    """Generate the pagination bar."""
    return """\
<div class="pagination-bar" id="paginationBar">
  <span class="page-info" id="pageInfo"></span>
  <div class="pagination-controls">
    <label>Per page:
      <select class="page-size-select" id="pageSize">
        <option value="10">10</option>
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
    return '<script>\n' + tusk_loader.load("tusk-dashboard-js").JS + '\n</script>'
