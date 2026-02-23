"""HTML generation layer for the tusk dashboard.

Contains all HTML templating functions: formatters, component generators, and
section builders. Imported by tusk-dashboard.py via importlib.

Not a standalone CLI command — used as a library module.
"""

import html
import importlib.util
import json
import logging
import sys
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path


log = logging.getLogger(__name__)


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


def _load_dashboard_js_module():
    """Import tusk-dashboard-js.py (hyphenated filename requires importlib)."""
    cached = sys.modules.get("tusk_dashboard_js")
    if cached is not None:
        return cached
    lib_path = Path(__file__).resolve().parent / "tusk-dashboard-js.py"
    spec = importlib.util.spec_from_file_location("tusk_dashboard_js", lib_path)
    if spec is None:
        raise FileNotFoundError(f"JS companion module not found: {lib_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tusk_dashboard_js"] = mod
    spec.loader.exec_module(mod)
    return mod


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
    return '<style>\n' + _load_dashboard_css_module().CSS + '\n</style>'


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
  <button class="tab-btn" data-tab="skills">Skill Runs</button>
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


def generate_global_tool_costs_section(tool_stats: list[dict]) -> str:
    """Generate a project-wide tool cost aggregate table for the Skills tab.

    Shows tool_name, total_calls, tokens_in, total_cost, and share of total
    across all task sessions. Returns a placeholder panel with onboarding
    instructions when no data is available.
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
    total_tokens_in = sum(r["tokens_in"] or 0 for r in tool_stats)

    rows_html = ""
    for r in tool_stats:
        cost = r["total_cost"] or 0
        calls = r["total_calls"] or 0
        tokens_in = r["tokens_in"] or 0
        pct = (cost / grand_total * 100) if grand_total > 0 else 0
        rows_html += (
            f'<tr>'
            f'<td style="font-weight:500;">{esc(r["tool_name"])}</td>'
            f'<td style="text-align:right;font-variant-numeric:tabular-nums;">{int(calls):,}</td>'
            f'<td style="text-align:right;font-variant-numeric:tabular-nums;">{format_tokens_compact(tokens_in)}</td>'
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
          <th style="text-align:right">Tokens In</th>
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
          <td style="text-align:right;font-variant-numeric:tabular-nums;">{format_tokens_compact(total_tokens_in)}</td>
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


def generate_velocity_section(velocity: list[dict]) -> str:
    """Generate the Velocity panel showing completed tasks per week with avg cost."""
    if not velocity:
        return """\
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header">Velocity</div>
  <p class="empty" style="padding: var(--sp-4);">No completed tasks yet. Velocity data will appear here once tasks are marked done.</p>
</div>"""

    total_tasks = sum(r["task_count"] for r in velocity)
    avg_per_week = total_tasks / len(velocity) if velocity else 0

    labels = [r["week"] for r in velocity]
    counts = [int(r["task_count"]) for r in velocity]

    chart_data = json.dumps({
        "labels": labels,
        "counts": counts,
    }).replace("</", "<\\/")

    table_rows = ""
    for r in velocity:
        avg_cost = r["avg_cost"] or 0
        table_rows += (
            f'<tr>'
            f'<td style="font-variant-numeric:tabular-nums;">{esc(r["week"])}</td>'
            f'<td style="text-align:right;font-variant-numeric:tabular-nums;">{int(r["task_count"])}</td>'
            f'<td style="text-align:right;font-variant-numeric:tabular-nums;">{format_cost(avg_cost)}</td>'
            f'</tr>\n'
        )

    return f"""\
<div class="panel" style="margin-bottom: var(--sp-6);">
  <div class="section-header">Velocity</div>
  <div class="kpi-grid" style="padding:var(--sp-4);margin-bottom:0;">
    <div class="kpi-card">
      <div class="kpi-label">Weeks Tracked</div>
      <div class="kpi-value">{len(velocity)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Tasks / Active Week (avg)</div>
      <div class="kpi-value">{avg_per_week:.1f}</div>
    </div>
  </div>
  <div style="display:flex;gap:var(--sp-6);padding:var(--sp-4);align-items:flex-start;flex-wrap:wrap;">
    <div class="dash-table-scroll" style="flex:1;min-width:220px;">
      <table>
        <thead>
          <tr>
            <th>Week</th>
            <th style="text-align:right">Tasks</th>
            <th style="text-align:right">Avg Cost</th>
          </tr>
        </thead>
        <tbody>
          {table_rows}
        </tbody>
      </table>
    </div>
    <div style="flex:2;min-width:200px;">
      <canvas id="velocityChart" height="150"></canvas>
    </div>
  </div>
</div>
<script>
(function() {{
  var vData = {chart_data};
  var canvas = document.getElementById('velocityChart');
  if (!canvas || !vData.labels.length) return;
  new Chart(canvas, {{
    type: 'bar',
    data: {{
      labels: vData.labels,
      datasets: [{{
        label: 'Tasks Completed',
        data: vData.counts,
        backgroundColor: '#3b82f699',
        borderColor: '#3b82f6',
        borderWidth: 1
      }}]
    }},
    options: {{
      responsive: true,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{ callbacks: {{ label: function(c) {{ return c.parsed.y + ' tasks'; }} }} }}
      }},
      scales: {{
        y: {{ beginAtZero: true, ticks: {{ stepSize: 1 }} }}
      }}
    }}
  }});
}})();
</script>"""


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
    return '<script>\n' + _load_dashboard_js_module().JS + '\n</script>'
