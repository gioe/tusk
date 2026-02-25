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

import importlib.util
import json
import logging
import os
import sys
import webbrowser
from datetime import datetime
from pathlib import Path


def _load_dashboard_html_module():
    """Import tusk-dashboard-html.py (hyphenated filename requires importlib)."""
    cached = sys.modules.get("tusk_dashboard_html")
    if cached is not None:
        return cached
    lib_path = Path(__file__).resolve().parent / "tusk-dashboard-html.py"
    spec = importlib.util.spec_from_file_location("tusk_dashboard_html", lib_path)
    if spec is None:
        raise FileNotFoundError(f"HTML companion module not found: {lib_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tusk_dashboard_html"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_dashboard_data_module():
    """Import tusk-dashboard-data.py (hyphenated filename requires importlib)."""
    cached = sys.modules.get("tusk_dashboard_data")
    if cached is not None:
        return cached
    lib_path = Path(__file__).resolve().parent / "tusk-dashboard-data.py"
    spec = importlib.util.spec_from_file_location("tusk_dashboard_data", lib_path)
    if spec is None:
        raise FileNotFoundError(f"Data companion module not found: {lib_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tusk_dashboard_data"] = mod
    spec.loader.exec_module(mod)
    return mod


log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Companion module imports
# ---------------------------------------------------------------------------

try:
    _data = _load_dashboard_data_module()
    _html = _load_dashboard_html_module()
except FileNotFoundError as _e:
    print(f"Error: {_e}\nRun 'tusk upgrade' to reinstall missing companion modules.", file=sys.stderr)
    sys.exit(1)

# Data-access layer
get_connection = _data.get_connection
fetch_task_metrics = _data.fetch_task_metrics
fetch_kpi_data = _data.fetch_kpi_data
fetch_cost_by_domain = _data.fetch_cost_by_domain
fetch_all_criteria = _data.fetch_all_criteria
fetch_task_dependencies = _data.fetch_task_dependencies
fetch_dag_tasks = _data.fetch_dag_tasks
fetch_edges = _data.fetch_edges
fetch_blockers = _data.fetch_blockers
fetch_skill_runs = _data.fetch_skill_runs
fetch_tool_call_stats_per_task = _data.fetch_tool_call_stats_per_task
fetch_tool_call_stats_per_skill_run = _data.fetch_tool_call_stats_per_skill_run
fetch_tool_call_stats_per_criterion = _data.fetch_tool_call_stats_per_criterion
fetch_tool_call_stats_global = _data.fetch_tool_call_stats_global
fetch_cost_trend = _data.fetch_cost_trend
fetch_cost_trend_daily = _data.fetch_cost_trend_daily
fetch_cost_trend_monthly = _data.fetch_cost_trend_monthly
fetch_complexity_metrics = _data.fetch_complexity_metrics
fetch_velocity = _data.fetch_velocity

# HTML generation layer
generate_css = _html.generate_css
generate_header = _html.generate_header
generate_footer = _html.generate_footer
generate_kpi_cards = _html.generate_kpi_cards
generate_skill_runs_section = _html.generate_skill_runs_section
generate_global_tool_costs_section = _html.generate_global_tool_costs_section
generate_charts_section = _html.generate_charts_section
generate_filter_bar = _html.generate_filter_bar
generate_table_header = _html.generate_table_header
generate_table_footer = _html.generate_table_footer
generate_pagination = _html.generate_pagination
generate_velocity_section = _html.generate_velocity_section
generate_complexity_section = _html.generate_complexity_section
generate_dag_section = _html.generate_dag_section
generate_js = _html.generate_js
generate_task_row = _html.generate_task_row


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
                  tool_call_global: list[dict] = None,
                  velocity: list[dict] = None) -> str:
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
            enriched = []
            for c in cl:
                ec = dict(c)
                ec["tool_stats"] = tool_stats_by_criterion.get(c["id"], [])
                enriched.append(ec)
            criteria_json[tid] = {
                "criteria": enriched,
            }
    _criteria_json_str = json.dumps(criteria_json).replace("</", "<\\/")
    criteria_script = f'<script>window.CRITERIA_DATA = {_criteria_json_str};</script>'

    # KPI cards
    kpi_html = generate_kpi_cards(kpi_data) if kpi_data else ""

    # Velocity card
    velocity_html = generate_velocity_section(velocity or [])

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
{css}
</head>
<body>

{header}

<div id="tab-dashboard" class="tab-panel active">
  <div class="container">
    {kpi_html}
    {velocity_html}
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
        # Velocity data (tasks completed per week)
        velocity = fetch_velocity(conn)
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
        tool_call_per_criterion, tool_call_global,
        velocity=velocity
    )
    log.debug("Generated %d bytes of HTML", len(html_content))

    # Write to tusk/<project>-dashboard.html (same dir as DB)
    db_dir = os.path.dirname(db_path)
    project_name = os.path.basename(os.path.dirname(db_dir))
    output_path = os.path.join(db_dir, f"{project_name}-dashboard.html")
    with open(output_path, "w") as f:
        f.write(html_content)
    log.debug("Wrote dashboard to %s", output_path)

    print(f"Dashboard written to {output_path}")

    # Open in browser
    webbrowser.open(f"file://{os.path.abspath(output_path)}")


if __name__ == "__main__":
    main()
