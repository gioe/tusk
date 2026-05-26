---
name: tusk-insights
description: Read-only DB health audit with interactive recommendations, plus on-demand HTML task dashboard generation
allowed-tools: Bash, Read
model: sonnet
---

# Tusk Insights

Two capabilities for inspecting the task database:

1. **HTML Dashboard** — generate and open a self-contained HTML view of per-task token counts, cost, and session metrics.
2. **Audit + Q&A** — read-only health audit followed by interactive recommendations. **Phase 1** runs a non-interactive audit across 6 categories, presenting findings as a structured report. **Phase 2** opens an interactive Q&A session for deeper exploration.

If the user invoked this skill asking for the dashboard (e.g. "/tusk-insights dashboard", "open the dashboard", "show me the HTML view"), run the **HTML Dashboard** flow below. Otherwise, run the **Audit + Q&A** flow.

---

## HTML Dashboard

Run:

```bash
tusk dashboard
```

Then confirm to the user that the dashboard has been generated and opened in their browser.

---

## Audit + Q&A

### Phase 1: Audit

#### Step 1: Load Config

```bash
tusk config
```

Parse the JSON. Note which arrays are empty — empty means no validation is configured for that column:

- `domains` → `[]` means skip domain orphan checks
- `agents` → `{}` means skip agent orphan checks

Hold onto the config values for Phase 2 (recommendations).

#### Step 2: Pre-Check Counts

Run the built-in audit command to get counts for all six categories:

```bash
tusk audit
```

This returns JSON with `config_fitness`, `task_hygiene`, `dependency_health`, `session_gaps`, `criteria_gaps`, and `scoring_gaps` counts. All six keys are always present even when the count is zero.

#### Step 3: Audit Report

For each category with a count **> 0**, load the companion file and run the corresponding detail queries:

```
Read file: <base_directory>/QUERIES.md
```

Present findings grouped by category with task IDs and summaries so the user can act on them. Categories with zero findings get a single line: `✓ No issues found`.

**Report format:**

```
## Tusk Health Audit

### 1. Config Fitness — {N} finding(s)
  ... detail from QUERIES.md ...

### 2. Task Hygiene — ✓ No issues found
  (skipped because count was 0)

### 3. Dependency Health — {N} finding(s)
  ... detail ...

(etc. for all 6 categories)
```

#### Step 4: Velocity Summary

Always run this step regardless of finding counts — velocity is informational, not a health issue.

Run the Velocity query:

```bash
tusk -header -column "
SELECT week, task_count, ROUND(avg_cost, 4) as avg_cost
FROM v_velocity
ORDER BY week DESC
LIMIT 8;
"
```

Present results in the audit report as:

```
### Velocity — Tasks Completed Per Week

week        task_count  avg_cost
----------  ----------  --------
2025-W08             3    0.1523
2025-W07             5    0.2100
...
```

If the query returns no rows, display:

```
### Velocity — No completed tasks recorded yet
```

#### Step 5: Worktree Pool Health

Always run this step regardless of audit findings — like Velocity, this is informational signal that surfaces a slow-accumulating problem (lingering worktrees consuming disk, registry rows pointing at deleted paths) the six audit categories don't cover. Originally added in TASK-478 because 11 stale worktrees went unnoticed for two weeks.

Run the insights command:

```bash
tusk insights --format text
```

The output is a pre-rendered section. Include it verbatim in the audit report under its own heading:

```
### Worktree Pool Health
- Reconcile-eligible rows: {N}
- Prune-eligible rows:     {N}
- Total disk usage:        {bytes}
- {accumulation_note}
```

Surface the findings as actions, not just numbers:

- **Reconcile-eligible rows > 0** — registered workspaces whose directories no longer exist on disk OR are missing from `git worktree list`. Recommend `tusk task-worktree list --format json` to inspect, then `tusk task-worktree prune` (with `--dry-run` first) to clean up.
- **Prune-eligible rows > 0** — same recommendation; prune resolves these.
- **Total disk usage** climbing without active in-progress tasks — workspaces from already-merged tasks aren't being torn down. Mention this as friction worth raising in `/retro` for the operator to investigate.
- **All zero** — show the section anyway with `✓ No worktree accumulation detected.` so operators see the surface exists.

---

### Phase 2: Interactive Q&A

After presenting the audit report, load the Q&A templates:

```
Read file: <base_directory>/RECOMMENDATIONS.md
```

Present the user with 5 discussion topics:

1. Domain alignment
2. Agent effectiveness
3. Workflow patterns
4. Backlog strategy
5. Free-form exploration

Ask which topic they'd like to explore. For each chosen topic, run the corresponding queries from RECOMMENDATIONS.md, analyze the results, and provide actionable recommendations.

The user can explore multiple topics or end the session at any time.
