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
import os
import sqlite3
import sys
import webbrowser
from datetime import datetime


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_task_metrics(conn: sqlite3.Connection) -> list[dict]:
    """Fetch per-task token and cost metrics from task_metrics view."""
    rows = conn.execute(
        """SELECT tm.id, tm.summary, tm.status,
                  tm.session_count,
                  COALESCE(tm.total_tokens_in, 0) as total_tokens_in,
                  COALESCE(tm.total_tokens_out, 0) as total_tokens_out,
                  COALESCE(tm.total_cost, 0) as total_cost,
                  s.model
           FROM task_metrics tm
           LEFT JOIN task_sessions s ON s.id = (
               SELECT s2.id FROM task_sessions s2
               WHERE s2.task_id = tm.id
               ORDER BY s2.cost_dollars DESC
               LIMIT 1
           )
           ORDER BY tm.total_cost DESC, tm.id ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


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


def generate_html(task_metrics: list[dict]) -> str:
    """Generate the full HTML dashboard."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Totals row
    total_tokens_in = sum(t["total_tokens_in"] for t in task_metrics)
    total_tokens_out = sum(t["total_tokens_out"] for t in task_metrics)
    total_cost = sum(t["total_cost"] for t in task_metrics)
    # Task rows
    task_rows = ""
    for t in task_metrics:
        has_data = t["session_count"] > 0
        muted = "" if has_data else ' class="muted"'
        task_rows += f"""<tr{muted}>
  <td class="col-id">#{t['id']}</td>
  <td class="col-summary">{esc(t['summary'])}</td>
  <td class="col-status"><span class="status-badge status-{esc(t['status']).lower().replace(' ', '-')}">{esc(t['status'])}</span></td>
  <td class="col-model">{esc(t.get('model') or '')}</td>
  <td class="col-tokens-in">{format_number(t['total_tokens_in'])}</td>
  <td class="col-tokens-out">{format_number(t['total_tokens_out'])}</td>
  <td class="col-cost">{format_cost(t['total_cost'])}</td>
</tr>\n"""

    # Empty state
    if not task_metrics:
        task_rows = '<tr><td colspan="7" class="empty">No tasks found. Run <code>tusk init</code> and add some tasks.</td></tr>'

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
  max-width: 1100px;
  margin: 0 auto;
  padding: 1.5rem;
}}

/* Table */
.panel {{
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow);
  overflow: hidden;
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
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Task</th>
          <th>Status</th>
          <th>Model</th>
          <th style="text-align:right">Tokens In</th>
          <th style="text-align:right">Tokens Out</th>
          <th style="text-align:right">Cost</th>
        </tr>
      </thead>
      <tbody>
        {task_rows}
      </tbody>
      <tfoot>
        <tr>
          <td colspan="4">Total</td>
          <td class="col-tokens-in">{format_number(total_tokens_in)}</td>
          <td class="col-tokens-out">{format_number(total_tokens_out)}</td>
          <td class="col-cost">{format_cost(total_cost)}</td>
        </tr>
      </tfoot>
    </table>
  </div>
</div>

</body>
</html>"""


def main():
    if len(sys.argv) < 3:
        print("Usage: tusk dashboard", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    # config_path accepted for dispatch consistency but unused currently
    # config_path = sys.argv[2]

    if not os.path.isfile(db_path):
        print(f"Error: Database not found at {db_path}", file=sys.stderr)
        print("Run 'tusk init' first.", file=sys.stderr)
        sys.exit(1)

    # Fetch data
    conn = get_connection(db_path)
    task_metrics = fetch_task_metrics(conn)
    conn.close()

    # Generate HTML
    html_content = generate_html(task_metrics)

    # Write to tusk/dashboard.html (same dir as DB)
    db_dir = os.path.dirname(db_path)
    output_path = os.path.join(db_dir, "dashboard.html")
    with open(output_path, "w") as f:
        f.write(html_content)

    print(f"Dashboard written to {output_path}")

    # Open in browser
    webbrowser.open(f"file://{os.path.abspath(output_path)}")


if __name__ == "__main__":
    main()
