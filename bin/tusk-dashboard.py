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

import json
import logging
import os
import sys
import webbrowser
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-dashboard-data.py and tusk-dashboard-html.py

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Companion module imports
# ---------------------------------------------------------------------------

try:
    _data = tusk_loader.load("tusk-dashboard-data")
    _html = tusk_loader.load("tusk-dashboard-html")
except FileNotFoundError as _e:
    print(f"Error: {_e}\nRun 'tusk upgrade' to reinstall missing companion modules.", file=sys.stderr)
    sys.exit(1)

# Data-access layer
get_connection = _data.get_connection
fetch_task_metrics = _data.fetch_task_metrics
fetch_kpi_data = _data.fetch_kpi_data
fetch_all_criteria = _data.fetch_all_criteria
fetch_task_dependencies = _data.fetch_task_dependencies
fetch_dag_tasks = _data.fetch_dag_tasks
fetch_edges = _data.fetch_edges
fetch_blockers = _data.fetch_blockers
fetch_skill_runs = _data.fetch_skill_runs
fetch_tool_call_stats_per_task = _data.fetch_tool_call_stats_per_task
fetch_tool_call_stats_per_skill_run = _data.fetch_tool_call_stats_per_skill_run
fetch_tool_call_stats_per_criterion = _data.fetch_tool_call_stats_per_criterion
fetch_tool_call_events_per_criterion = _data.fetch_tool_call_events_per_criterion
fetch_tool_call_stats_global = _data.fetch_tool_call_stats_global
fetch_cost_trend = _data.fetch_cost_trend
fetch_cost_trend_daily = _data.fetch_cost_trend_daily
fetch_cost_trend_monthly = _data.fetch_cost_trend_monthly
fetch_cost_per_user_prompt_trend = _data.fetch_cost_per_user_prompt_trend
fetch_hourly_cost = _data.fetch_hourly_cost
fetch_dow_hour_heatmap = _data.fetch_dow_hour_heatmap
fetch_cost_scatter_data = _data.fetch_cost_scatter_data
fetch_model_performance = _data.fetch_model_performance
fetch_rework_rate = _data.fetch_rework_rate
# HTML generation layer
generate_css = _html.generate_css
generate_header = _html.generate_header
generate_footer = _html.generate_footer
generate_skill_runs_section = _html.generate_skill_runs_section

generate_cost_trend_section = _html.generate_cost_trend_section
generate_cost_per_user_prompt_section = _html.generate_cost_per_user_prompt_section
generate_hourly_cost_section = _html.generate_hourly_cost_section
generate_dow_hour_heatmap_section = _html.generate_dow_hour_heatmap_section
generate_cost_scatter_section = _html.generate_cost_scatter_section
generate_models_section = _html.generate_models_section
generate_rework_rate_section = _html.generate_rework_rate_section
generate_filter_bar = _html.generate_filter_bar
generate_table_header = _html.generate_table_header
generate_pagination = _html.generate_pagination
generate_dag_section = _html.generate_dag_section
generate_js = _html.generate_js
generate_task_row = _html.generate_task_row


def _tz_label(offset_minutes: int) -> str:
    """Return a UTC±H or UTC±H:MM label for the given offset in minutes."""
    h, m = divmod(abs(offset_minutes), 60)
    sign = "+" if offset_minutes >= 0 else "-"
    if m:
        return f"UTC{sign}{h}:{m:02d}"
    return f"UTC{sign}{h}"


def generate_html(task_metrics: list[dict],
                  cost_trend: list[dict] = None, all_criteria: dict[int, list[dict]] = None,
                  cost_trend_daily: list[dict] = None, cost_trend_monthly: list[dict] = None,
                  cost_per_user_prompt: list[dict] = None,
                  task_deps: dict[int, dict] = None,
                  version: str = "",
                  dag_tasks: list[dict] = None, dag_edges: list[dict] = None,
                  dag_blockers: list[dict] = None, skill_runs: list[dict] = None,
                  tool_call_per_task: list[dict] = None,
                  tool_call_per_skill_run: list[dict] = None,
                  tool_call_per_criterion: list[dict] = None,
                  tool_call_global: list[dict] = None,
                  tool_call_events_per_criterion: list[dict] = None,
                  utc_offset_minutes: int = 0,
                  hourly_cost: list[dict] = None,
                  dow_hour_heatmap: list[dict] = None,
                  cost_scatter_data: list[dict] = None,
                  model_performance: dict = None,
                  rework_rate: list[dict] = None,
                  project_name: str = "Tusk") -> str:
    """Generate the full HTML dashboard by composing sub-functions."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tz_label = _tz_label(utc_offset_minutes)

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

    # Build per-criterion tool call events lookup (individual call rows)
    events_by_criterion: dict[int, list[dict]] = {}
    for r in (tool_call_events_per_criterion or []):
        cid = r["criterion_id"]
        events_by_criterion.setdefault(cid, []).append(r)

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
        task_rows = '<tr><td colspan="12" class="empty">No tasks found. Run <code>tusk init</code> and add some tasks.</td></tr>'

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
                ec["tool_events"] = events_by_criterion.get(c["id"], [])
                enriched.append(ec)
            criteria_json[tid] = {
                "criteria": enriched,
                "task_tool_stats": tool_stats_by_task.get(tid, []),
                "task_total_cost": t["total_cost"],
            }
    _criteria_json_str = json.dumps(criteria_json).replace("</", "<\\/")
    criteria_script = f'<script>window.CRITERIA_DATA = {_criteria_json_str};</script>'

    hourly_cost_json = json.dumps(hourly_cost or []).replace("</", "<\\/")
    dow_hour_heatmap_json = json.dumps(dow_hour_heatmap or []).replace("</", "<\\/")
    cost_scatter_json = json.dumps(cost_scatter_data or []).replace("</", "<\\/")

    # All Runs table → Skills tab
    skill_runs_html = generate_skill_runs_section(skill_runs or [], tool_stats_by_run)

    # Unified cost trend chart (Tasks/Skills toggle → Cost tab)
    cost_trend_html = generate_cost_trend_section(
        cost_trend or [], cost_trend_daily or [], cost_trend_monthly or [],
        skill_runs or []
    )

    # Cost-per-user-prompt weekly trend (→ Cost tab, after trend)
    cost_per_user_prompt_html = generate_cost_per_user_prompt_section(
        cost_per_user_prompt or []
    )

    # Hour of day cost bar chart (→ Cost tab, after trend)
    hourly_cost_html = generate_hourly_cost_section()

    # Cost scatter plot (→ Cost tab, between hourly and heatmap)
    cost_scatter_html = generate_cost_scatter_section()

    # Day-of-week × hour heatmap (→ Cost tab, after scatter)
    dow_hour_heatmap_html = generate_dow_hour_heatmap_section()

    # DAG section
    dag_html = generate_dag_section(
        dag_tasks or [], dag_edges or [], dag_blockers or []
    )

    # Models tab (per-model rollups, complexity table, trend chart)
    models_html = generate_models_section(model_performance or {})
    rework_rate_html = generate_rework_rate_section(rework_rate or [])

    css = generate_css()
    header = generate_header(now, tz_label, project_name)
    footer = generate_footer(now, version)
    filter_bar = generate_filter_bar()
    table_header = generate_table_header()
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
<title>{_html.esc(project_name)} &mdash; Task Metrics</title>
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
    <div class="panel">
      {filter_bar}
      <table id="metricsTable">
        {table_header}
        <tbody id="metricsBody">
          {task_rows}
        </tbody>
      </table>
      {pagination}
    </div>
  </div>
</div>

<div id="tab-dag" class="tab-panel dag-tab-panel">
  {dag_html}
</div>

<div id="tab-skills" class="tab-panel">
  <div class="container">
    {skill_runs_html}
  </div>
</div>

<div id="tab-cost" class="tab-panel">
  <div class="container">
    {cost_trend_html}
    {cost_per_user_prompt_html}
    {hourly_cost_html}
    {cost_scatter_html}
    {dow_hour_heatmap_html}
  </div>
</div>

<div id="tab-models" class="tab-panel">
  <div class="container">
    {models_html}
    {rework_rate_html}
  </div>
</div>

{footer}

{criteria_script}
<script>window.__tuskTzOffset = {utc_offset_minutes};</script>
<script>window.__tuskHourlyCost = {hourly_cost_json};</script>
<script>window.__tuskDowHourHeatmap = {dow_hour_heatmap_json};</script>
<script>window.__tuskCostScatter = {cost_scatter_json};</script>
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

    # Local timezone offset (minutes east of UTC) for date bucketing
    utc_offset_minutes = int(datetime.now().astimezone().utcoffset().total_seconds() / 60)
    log.debug("UTC offset: %d minutes", utc_offset_minutes)

    # Fetch data
    conn = get_connection(db_path)
    try:
        task_metrics = fetch_task_metrics(conn)
        cost_trend = fetch_cost_trend(conn, utc_offset_minutes)
        cost_trend_daily = fetch_cost_trend_daily(conn, utc_offset_minutes)
        cost_trend_monthly = fetch_cost_trend_monthly(conn, utc_offset_minutes)
        cost_per_user_prompt = fetch_cost_per_user_prompt_trend(conn, utc_offset_minutes)
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
        # Per-criterion individual tool call events (for timeline visualization)
        tool_call_events_per_criterion = fetch_tool_call_events_per_criterion(conn)
        # Project-wide tool call stats (for Skills tab aggregate view)
        tool_call_global = fetch_tool_call_stats_global(conn)
        # Hourly and day-of-week/hour cost aggregations
        hourly_cost = fetch_hourly_cost(conn, utc_offset_minutes)
        dow_hour_heatmap = fetch_dow_hour_heatmap(conn, utc_offset_minutes)
        cost_scatter_data = fetch_cost_scatter_data(conn, utc_offset_minutes)
        # Per-model rollups for the Models tab
        model_performance = fetch_model_performance(conn, utc_offset_minutes)
        # Per-model rework rate (shipped feature/bug tasks that later got a follow-up)
        rework_rate = fetch_rework_rate(conn)
    finally:
        conn.close()

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

    # Derive project name for use in HTML header and output filename
    db_dir = os.path.dirname(db_path)
    project_name = os.path.basename(os.path.dirname(db_dir))

    # Generate HTML
    html_content = generate_html(
        task_metrics, cost_trend, all_criteria,
        cost_trend_daily, cost_trend_monthly,
        cost_per_user_prompt=cost_per_user_prompt,
        task_deps=task_deps,
        version=version,
        dag_tasks=dag_tasks, dag_edges=dag_edges,
        dag_blockers=dag_blockers, skill_runs=skill_runs,
        tool_call_per_task=tool_call_per_task,
        tool_call_per_skill_run=tool_call_per_skill_run,
        tool_call_per_criterion=tool_call_per_criterion,
        tool_call_global=tool_call_global,
        tool_call_events_per_criterion=tool_call_events_per_criterion,
        utc_offset_minutes=utc_offset_minutes,
        hourly_cost=hourly_cost,
        dow_hour_heatmap=dow_hour_heatmap,
        cost_scatter_data=cost_scatter_data,
        model_performance=model_performance,
        rework_rate=rework_rate,
        project_name=project_name,
    )
    log.debug("Generated %d bytes of HTML", len(html_content))
    output_path = os.path.join(db_dir, f"{project_name}-dashboard.html")
    with open(output_path, "w") as f:
        f.write(html_content)
    log.debug("Wrote dashboard to %s", output_path)

    print(f"Dashboard written to {output_path}")

    # Open in browser
    webbrowser.open(f"file://{os.path.abspath(output_path)}")


if __name__ == "__main__":
    main()
