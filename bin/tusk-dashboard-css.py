"""CSS stylesheet for tusk-dashboard.py.

Extracted from generate_css() to reduce the main file size.
"""

CSS: str = """\
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
.section-header--bordered {
  border-top: 1px solid var(--border);
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
}

/* Utility: muted text color — pairs with the --text-muted CSS variable */
.text-muted {
  color: var(--text-muted);
}

.dash-table-scroll {
  overflow-x: auto;
  padding: var(--sp-4);
}

.dash-chart-wrap {
  padding: var(--sp-4);
}

/* Tool call breakdown panel inside task row expansion */
.tc-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}
.tc-table th {
  text-align: left;
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 2px solid var(--border);
  font-weight: 600;
  white-space: nowrap;
}
.tc-table td {
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
.tc-row:last-child td {
  border-bottom: none;
}
.tc-task-panel > summary::-webkit-details-marker { display: none; }
.tc-task-panel > summary::marker { display: none; }
.tc-task-panel--bordered { border-top: 1px solid var(--border); }
"""
