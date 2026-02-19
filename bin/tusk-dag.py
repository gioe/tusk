#!/usr/bin/env python3
"""Generate a self-contained HTML page with a Mermaid.js DAG of task dependencies.

Renders tasks as nodes colored by status, shaped by complexity, with edges
showing dependency relationships. Includes a click-to-inspect sidebar with
per-task metrics.

Called by the tusk wrapper:
    tusk dag [--all] [--debug]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (unused)
    sys.argv[3:] — flags (--all, --debug)
"""

import json
import logging
import os
import sqlite3
import sys
import webbrowser
from collections import defaultdict, deque
from datetime import datetime

log = logging.getLogger(__name__)


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_tasks(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all tasks with metrics and criteria counts."""
    log.debug("Querying task_metrics view with criteria counts")
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
    log.debug("Fetched %d tasks", len(result))
    return result


def fetch_edges(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all dependency edges."""
    log.debug("Querying task_dependencies")
    rows = conn.execute(
        """SELECT task_id, depends_on_id, relationship_type
           FROM task_dependencies"""
    ).fetchall()
    result = [dict(r) for r in rows]
    log.debug("Fetched %d edges", len(result))
    return result


def fetch_blockers(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all external blockers."""
    log.debug("Querying external_blockers")
    rows = conn.execute(
        """SELECT id, task_id, description, blocker_type, is_resolved
           FROM external_blockers"""
    ).fetchall()
    result = [dict(r) for r in rows]
    log.debug("Fetched %d blockers", len(result))
    return result


def filter_nodes(tasks: list[dict], edges: list[dict], blockers: list[dict], show_all: bool):
    """Filter tasks, edges, and blockers to visible set.

    Default: all To Do + In Progress tasks, plus Done tasks with >= 1 edge.
    --all: additionally include isolated Done tasks.
    Edges are filtered to only those between visible nodes.
    Blockers are filtered to only those attached to visible tasks.
    """
    # Collect task IDs that appear in any edge
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

    # Prune connected components where every task is Done
    if not show_all:
        # Build adjacency graph from edges between visible nodes
        adj = defaultdict(set)
        for e in edges:
            a, b = e["task_id"], e["depends_on_id"]
            if a in visible_ids and b in visible_ids:
                adj[a].add(b)
                adj[b].add(a)

        # Find connected components via BFS; mark all-Done ones for removal
        status_map = {t["id"]: t["status"] for t in visible_tasks}
        visited = set()
        remove_ids = set()
        for tid in visible_ids:
            if tid in visited:
                continue
            queue = deque([tid])
            component = []
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

    # Filter edges to only those between visible nodes
    visible_edges = [
        e for e in edges
        if e["task_id"] in visible_ids and e["depends_on_id"] in visible_ids
    ]

    # Filter blockers to only those attached to visible tasks
    visible_blockers = [b for b in blockers if b["task_id"] in visible_ids]

    log.debug("Visible: %d tasks, %d edges, %d blockers", len(visible_tasks), len(visible_edges), len(visible_blockers))
    return visible_tasks, visible_edges, visible_blockers


def build_mermaid(tasks: list[dict], edges: list[dict], blockers: list[dict]) -> str:
    """Build Mermaid graph definition."""
    lines = ["graph LR"]

    # classDef for status colors
    lines.append('    classDef todo fill:#3b82f6,stroke:#2563eb,color:#fff')
    lines.append('    classDef inprogress fill:#f59e0b,stroke:#d97706,color:#fff')
    lines.append('    classDef done fill:#22c55e,stroke:#16a34a,color:#fff')
    lines.append('    classDef blocker fill:#ef4444,stroke:#dc2626,color:#fff')
    lines.append('    classDef blockerResolved fill:#9ca3af,stroke:#6b7280,color:#fff')

    # Node definitions
    for t in tasks:
        node_id = "T" + str(t["id"])
        summary = t["summary"] or ""
        if len(summary) > 40:
            summary = summary[:37] + "..."
        # Escape quotes for Mermaid
        summary = summary.replace('"', "'")
        label = "#" + str(t["id"]) + ": " + summary
        complexity = t["complexity"] or "S"

        # Shape by complexity — avoid f-strings for hexagon braces
        if complexity in ("XS", "S"):
            node_def = node_id + '["' + label + '"]'
        elif complexity == "M":
            node_def = node_id + '("' + label + '")'
        else:
            # L/XL → hexagon: {{label}}
            node_def = node_id + '{{"' + label + '"}}'

        lines.append("    " + node_def)

        # Apply class based on status
        status = t["status"]
        if status == "To Do":
            lines.append("    class " + node_id + " todo")
        elif status == "In Progress":
            lines.append("    class " + node_id + " inprogress")
        elif status == "Done":
            lines.append("    class " + node_id + " done")

    # Blocker node definitions — octagon shape via double brackets
    for b in blockers:
        node_id = "B" + str(b["id"])
        desc = b["description"] or ""
        if len(desc) > 35:
            desc = desc[:32] + "..."
        desc = desc.replace('"', "'")
        btype = b["blocker_type"] or "external"
        label = btype + ": " + desc
        # Mermaid stadium shape (rounded ends) via ([...]) for blockers
        # Using triple braces for a double-bracket "subroutine" shape is not
        # widely supported, so use >...] flag shape for visual distinction
        node_def = node_id + '>"' + label + '"]'
        lines.append("    " + node_def)

        if b["is_resolved"]:
            lines.append("    class " + node_id + " blockerResolved")
        else:
            lines.append("    class " + node_id + " blocker")

    # Edge definitions: depends_on_id --> task_id (prerequisite → dependent)
    for e in edges:
        src = "T" + str(e["depends_on_id"])
        dst = "T" + str(e["task_id"])
        if e["relationship_type"] == "contingent":
            lines.append("    " + src + " -.-> " + dst)
        else:
            lines.append("    " + src + " --> " + dst)

    # Blocker edges: blocker -..->|blocks| task (red dashed)
    for b in blockers:
        src = "B" + str(b["id"])
        dst = "T" + str(b["task_id"])
        lines.append("    " + src + " -.-x " + dst)

    # Click callbacks for tasks
    for t in tasks:
        node_id = "T" + str(t["id"])
        lines.append('    click ' + node_id + ' showSidebar')

    # Click callbacks for blockers
    for b in blockers:
        node_id = "B" + str(b["id"])
        lines.append('    click ' + node_id + ' showBlockerSidebar')

    return "\n".join(lines)


def format_cost(c) -> str:
    if c is None or c == 0:
        return "$0.00"
    return f"${c:,.2f}"


def format_duration(seconds) -> str:
    if seconds is None or seconds == 0:
        return "0m"
    hours = int(seconds) // 3600
    minutes = (int(seconds) % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_number(n) -> str:
    if n is None:
        return "0"
    return f"{int(n):,}"


def generate_html(tasks: list[dict], edges: list[dict], blockers: list[dict]) -> str:
    """Generate the full HTML page with Mermaid DAG and sidebar."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Empty state: no tasks at all
    if not tasks:
        return _empty_page(now, "No tasks found. Run <code>tusk init</code> and add some tasks.")

    mermaid_def = build_mermaid(tasks, edges, blockers)

    # Build task data JSON for sidebar lookups
    task_data = {}
    # Group blockers by task_id for sidebar display
    blockers_by_task = defaultdict(list)
    for b in blockers:
        blockers_by_task[b["task_id"]].append({
            "id": b["id"],
            "description": b["description"],
            "blocker_type": b["blocker_type"],
            "is_resolved": b["is_resolved"],
        })

    for t in tasks:
        task_blockers = blockers_by_task.get(t["id"], [])
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
            "blockers": task_blockers,
        }

    # Build blocker data JSON for blocker node click sidebar
    blocker_data = {}
    for b in blockers:
        blocker_data[b["id"]] = {
            "id": b["id"],
            "task_id": b["task_id"],
            "description": b["description"],
            "blocker_type": b["blocker_type"],
            "is_resolved": b["is_resolved"],
        }

    task_json = json.dumps(task_data)
    blocker_json = json.dumps(blocker_data)
    # Prevent </script> injection
    task_json = task_json.replace("</", "<\\/")
    blocker_json = blocker_json.replace("</", "<\\/")

    has_edges = len(edges) > 0 or len(blockers) > 0
    hint = "" if has_edges else '<p class="hint">No dependencies yet. Use <code>tusk deps add</code> to connect tasks.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tusk — Dependency DAG</title>
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
  height: 100vh;
  display: flex;
  flex-direction: column;
}}

.header {{
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border);
  padding: 1rem 2rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
  box-shadow: var(--shadow);
  flex-shrink: 0;
}}

.header h1 {{
  font-size: 1.5rem;
  font-weight: 700;
}}

.header .timestamp {{
  color: var(--text-muted);
  font-size: 0.85rem;
}}

.main {{
  display: flex;
  flex: 1;
  overflow: hidden;
}}

.dag-panel {{
  flex: 1;
  overflow: auto;
  padding: 1.5rem;
  display: flex;
  flex-direction: column;
}}

.mermaid {{
  flex: 1;
}}

.sidebar {{
  width: 320px;
  background: var(--bg-panel);
  border-left: 1px solid var(--border);
  box-shadow: var(--shadow);
  overflow-y: auto;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
}}

.sidebar-placeholder {{
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 1;
  color: var(--text-muted);
  font-size: 0.9rem;
  padding: 2rem;
  text-align: center;
}}

.sidebar-content {{
  display: none;
  padding: 1.5rem;
}}

.sidebar-content.active {{
  display: block;
}}

.sidebar-content h2 {{
  font-size: 1.1rem;
  font-weight: 700;
  margin-bottom: 1rem;
  word-break: break-word;
}}

.metric {{
  display: flex;
  justify-content: space-between;
  padding: 0.4rem 0;
  border-bottom: 1px solid var(--border);
  font-size: 0.85rem;
}}

.metric:last-child {{
  border-bottom: none;
}}

.metric-label {{
  color: var(--text-muted);
  font-weight: 500;
}}

.metric-value {{
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}}

.legend {{
  padding: 1rem 1.5rem;
  border-top: 1px solid var(--border);
  font-size: 0.75rem;
  color: var(--text-muted);
  flex-shrink: 0;
}}

.legend-title {{
  font-weight: 600;
  margin-bottom: 0.3rem;
}}

.legend-row {{
  display: flex;
  gap: 1rem;
  flex-wrap: wrap;
  margin-bottom: 0.2rem;
}}

.legend-item {{
  display: flex;
  align-items: center;
  gap: 0.3rem;
}}

.legend-swatch {{
  width: 12px;
  height: 12px;
  border-radius: 3px;
  flex-shrink: 0;
}}

.hint {{
  text-align: center;
  color: var(--text-muted);
  font-size: 0.85rem;
  padding: 0.5rem;
}}

.hint code {{
  background: var(--hover);
  padding: 0.15rem 0.4rem;
  border-radius: 3px;
  font-size: 0.85em;
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

.blocker-badge {{
  font-size: 0.7rem;
  font-weight: 600;
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
  white-space: nowrap;
}}

.blocker-open {{
  background: #fef2f2;
  color: #dc2626;
}}

.blocker-resolved {{
  background: #f3f4f6;
  color: #6b7280;
}}

@media (prefers-color-scheme: dark) {{
  .blocker-open {{
    background: #7f1d1d;
    color: #fca5a5;
  }}
  .blocker-resolved {{
    background: #374151;
    color: #9ca3af;
  }}
}}

.blocker-item {{
  padding: 0.4rem 0;
  border-bottom: 1px solid var(--border);
  font-size: 0.85rem;
}}

.blocker-item:last-child {{
  border-bottom: none;
}}

.blocker-header {{
  display: flex;
  align-items: center;
  gap: 0.4rem;
  margin-bottom: 0.2rem;
}}

.blocker-type {{
  font-size: 0.75rem;
  color: var(--text-muted);
}}

.blocker-desc {{
  font-size: 0.8rem;
  color: var(--text);
  word-break: break-word;
}}
</style>
</head>
<body>

<div class="header">
  <h1>Tusk — Dependency DAG</h1>
  <span class="timestamp">Generated {now}</span>
</div>

<div class="main">
  <div class="dag-panel">
    <pre class="mermaid">
{mermaid_def}
    </pre>
    {hint}
    <div class="legend">
      <div class="legend-title">Legend</div>
      <div class="legend-row">
        <span class="legend-item"><span class="legend-swatch" style="background:#3b82f6"></span> To Do</span>
        <span class="legend-item"><span class="legend-swatch" style="background:#f59e0b"></span> In Progress</span>
        <span class="legend-item"><span class="legend-swatch" style="background:#22c55e"></span> Done</span>
        <span class="legend-item"><span class="legend-swatch" style="background:#ef4444"></span> Blocker</span>
        <span class="legend-item"><span class="legend-swatch" style="background:#9ca3af"></span> Resolved Blocker</span>
      </div>
      <div class="legend-row">
        <span class="legend-item">[rect] = XS/S</span>
        <span class="legend-item">(rounded) = M</span>
        <span class="legend-item">&#x2B21; hexagon = L/XL</span>
        <span class="legend-item">&#x25B7; flag = blocker</span>
      </div>
      <div class="legend-row">
        <span class="legend-item">&mdash;&mdash;&gt; blocks</span>
        <span class="legend-item">- - -&gt; contingent</span>
        <span class="legend-item">-&middot;-x blocker</span>
      </div>
    </div>
  </div>

  <div class="sidebar">
    <div class="sidebar-placeholder" id="placeholder">
      Click a node to inspect task details
    </div>
    <div class="sidebar-content" id="sidebar-content">
      <h2 id="sb-title"></h2>
      <div id="sb-metrics"></div>
    </div>
  </div>
</div>

<script>
var TASK_DATA = {task_json};
var BLOCKER_DATA = {blocker_json};

function showSidebar(nodeId) {{
  var id = parseInt(nodeId.replace('T', ''), 10);
  var t = TASK_DATA[id];
  if (!t) return;

  document.getElementById('placeholder').style.display = 'none';
  var content = document.getElementById('sidebar-content');
  content.classList.add('active');

  document.getElementById('sb-title').textContent = '#' + t.id + ': ' + t.summary;

  var statusClass = 'status-' + t.status.toLowerCase().replace(' ', '-');
  var criteria = t.criteria_total > 0 ? t.criteria_done + '/' + t.criteria_total : '—';

  var metricsHtml = '<div class="metric"><span class="metric-label">Status</span><span class="metric-value"><span class="status-badge ' + statusClass + '">' + t.status + '</span></span></div>'
    + '<div class="metric"><span class="metric-label">Priority</span><span class="metric-value">' + (t.priority || '—') + '</span></div>'
    + '<div class="metric"><span class="metric-label">Complexity</span><span class="metric-value">' + (t.complexity || '—') + '</span></div>'
    + '<div class="metric"><span class="metric-label">Domain</span><span class="metric-value">' + (t.domain || '—') + '</span></div>'
    + '<div class="metric"><span class="metric-label">Type</span><span class="metric-value">' + (t.task_type || '—') + '</span></div>'
    + '<div class="metric"><span class="metric-label">Priority Score</span><span class="metric-value">' + (t.priority_score != null ? t.priority_score : '—') + '</span></div>'
    + '<div class="metric"><span class="metric-label">Sessions</span><span class="metric-value">' + t.sessions + '</span></div>'
    + '<div class="metric"><span class="metric-label">Tokens In</span><span class="metric-value">' + t.tokens_in + '</span></div>'
    + '<div class="metric"><span class="metric-label">Tokens Out</span><span class="metric-value">' + t.tokens_out + '</span></div>'
    + '<div class="metric"><span class="metric-label">Cost</span><span class="metric-value">' + t.cost + '</span></div>'
    + '<div class="metric"><span class="metric-label">Duration</span><span class="metric-value">' + t.duration + '</span></div>'
    + '<div class="metric"><span class="metric-label">Criteria</span><span class="metric-value">' + criteria + '</span></div>';

  // Blocker details
  if (t.blockers && t.blockers.length > 0) {{
    metricsHtml += '<div style="margin-top:0.75rem;font-weight:700;font-size:0.85rem;">External Blockers</div>';
    for (var i = 0; i < t.blockers.length; i++) {{
      var b = t.blockers[i];
      var resolvedBadge = b.is_resolved
        ? '<span class="blocker-badge blocker-resolved">Resolved</span>'
        : '<span class="blocker-badge blocker-open">Open</span>';
      var typeLabel = b.blocker_type || 'external';
      metricsHtml += '<div class="blocker-item">'
        + '<div class="blocker-header">' + resolvedBadge + ' <span class="blocker-type">' + typeLabel + '</span></div>'
        + '<div class="blocker-desc">' + b.description + '</div>'
        + '</div>';
    }}
  }}

  document.getElementById('sb-metrics').innerHTML = metricsHtml;
}}

function showBlockerSidebar(nodeId) {{
  var id = parseInt(nodeId.replace('B', ''), 10);
  var b = BLOCKER_DATA[id];
  if (!b) return;

  document.getElementById('placeholder').style.display = 'none';
  var content = document.getElementById('sidebar-content');
  content.classList.add('active');

  var resolvedText = b.is_resolved ? 'Resolved' : 'Open';
  document.getElementById('sb-title').textContent = 'Blocker #' + b.id;

  var resolvedBadge = b.is_resolved
    ? '<span class="blocker-badge blocker-resolved">Resolved</span>'
    : '<span class="blocker-badge blocker-open">Open</span>';

  var metricsHtml = '<div class="metric"><span class="metric-label">Status</span><span class="metric-value">' + resolvedBadge + '</span></div>'
    + '<div class="metric"><span class="metric-label">Type</span><span class="metric-value">' + (b.blocker_type || 'external') + '</span></div>'
    + '<div class="metric"><span class="metric-label">Blocks Task</span><span class="metric-value">#' + b.task_id + '</span></div>'
    + '<div style="margin-top:0.75rem;font-size:0.85rem;">' + b.description + '</div>';

  document.getElementById('sb-metrics').innerHTML = metricsHtml;
}}
</script>
<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
<script>
  var isDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  mermaid.initialize({{
    startOnLoad: true,
    securityLevel: 'loose',
    theme: isDark ? 'dark' : 'default'
  }});
</script>
</body>
</html>"""


def _empty_page(now: str, message: str) -> str:
    """Generate a minimal page for empty states."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Tusk — Dependency DAG</title>
<style>
:root {{
  --bg: #f8fafc; --bg-panel: #ffffff; --text: #0f172a;
  --text-muted: #94a3b8; --border: #e2e8f0;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0f172a; --bg-panel: #1e293b; --text: #f1f5f9;
    --text-muted: #64748b; --border: #334155;
  }}
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.5;
  display: flex; align-items: center; justify-content: center; height: 100vh;
}}
.empty {{ text-align: center; color: var(--text-muted); font-size: 1.1rem; }}
.empty code {{
  background: var(--bg-panel); padding: 0.15rem 0.4rem;
  border-radius: 3px; font-size: 0.85em;
}}
</style>
</head>
<body>
<div class="empty"><p>{message}</p></div>
</body>
</html>"""


def main():
    argv = sys.argv[1:]
    debug = "--debug" in argv
    show_all = "--all" in argv
    flags = {"--debug", "--all"}
    argv = [a for a in argv if a not in flags]

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="[debug] %(message)s",
        stream=sys.stderr,
    )

    if len(argv) < 2:
        print("Usage: tusk dag [--all] [--debug]", file=sys.stderr)
        sys.exit(1)

    db_path = argv[0]
    # config_path accepted for dispatch consistency but unused
    # config_path = argv[1]
    log.debug("DB path: %s", db_path)

    if not os.path.isfile(db_path):
        print(f"Error: Database not found at {db_path}", file=sys.stderr)
        print("Run 'tusk init' first.", file=sys.stderr)
        sys.exit(1)

    conn = get_connection(db_path)
    tasks = fetch_tasks(conn)
    edges = fetch_edges(conn)
    blockers = fetch_blockers(conn)
    conn.close()

    visible_tasks, visible_edges, visible_blockers = filter_nodes(tasks, edges, blockers, show_all)

    html_content = generate_html(visible_tasks, visible_edges, visible_blockers)
    log.debug("Generated %d bytes of HTML", len(html_content))

    db_dir = os.path.dirname(db_path)
    output_path = os.path.join(db_dir, "dag.html")
    with open(output_path, "w") as f:
        f.write(html_content)
    log.debug("Wrote DAG to %s", output_path)

    print(f"DAG written to {output_path}")
    webbrowser.open(f"file://{os.path.abspath(output_path)}")


if __name__ == "__main__":
    main()
