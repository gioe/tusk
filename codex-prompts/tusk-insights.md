# Tusk Insights — DB Health Audit + HTML Dashboard (Codex)

Two capabilities for inspecting the task database:

1. **HTML Dashboard** — generate and open a self-contained HTML view of
   per-task token counts, cost, and session metrics.
2. **Audit + Q&A** — read-only health audit followed by interactive
   recommendations. **Phase 1** runs a non-interactive audit across 6
   categories, presenting findings as a structured report. **Phase 2** opens
   an interactive Q&A session for deeper exploration.

If the user invoked this prompt asking for the dashboard (e.g.
"/tusk-insights dashboard", "open the dashboard", "show me the HTML view"),
run the **HTML Dashboard** flow below. Otherwise, run the **Audit + Q&A**
flow.

> **Conventions:** Run `tusk conventions search <topic>` for project rules.
> Do not restate convention text inline — it drifts from the DB.

---

## HTML Dashboard

Run:

```bash
tusk dashboard
```

Then confirm to the user that the dashboard has been generated and opened
in their browser.

---

## Audit + Q&A

### Phase 1: Audit

#### Step 1: Load Config

```bash
tusk config
```

Parse the JSON. Note which arrays are empty — empty means no validation is
configured for that column:

- `domains` → `[]` means skip domain orphan checks
- `agents` → `{}` means skip agent orphan checks

Hold onto the config values for Phase 2 (recommendations).

#### Step 2: Pre-Check Counts

Run the built-in audit command to get counts for all six categories:

```bash
tusk audit
```

This returns JSON with `config_fitness`, `task_hygiene`,
`dependency_health`, `session_gaps`, `criteria_gaps`, and `scoring_gaps`
counts. All six keys are always present even when the count is zero.

#### Step 3: Audit Report

For each category with a count **> 0**, run the corresponding detail queries.
The full set of category-specific queries lives in
`.codex/prompts/tusk-insights-queries.md` if shipped, otherwise derive them
from the category names — common follow-ups include:

- **config_fitness** — list disabled validation arrays, missing domains, agent
  orphans.
- **task_hygiene** — list `Done` tasks with no `closed_reason`, deferred
  tasks past `expires_at`, criteria with no commit hash.
- **dependency_health** — list cycles, dangling FKs, contingent edges where
  the upstream is closed but child is open.
- **session_gaps** — list open sessions older than 24h with no progress, or
  closed sessions with `active_seconds = 0`.
- **criteria_gaps** — list tasks with zero criteria, or criteria types with
  empty `verification_spec` where required.
- **scoring_gaps** — list tasks missing `priority` or `complexity`, NULL
  `priority_score` rows, missing WSJF inputs.

Use `tusk task-list --format json` and `tusk -header -column "<sql>"` for the
underlying queries.

Present findings grouped by category with task IDs and summaries so the
user can act on them. Categories with zero findings get a single line:
`✓ No issues found`.

**Report format:**

```
## Tusk Health Audit

### 1. Config Fitness — {N} finding(s)
  ... detail ...

### 2. Task Hygiene — ✓ No issues found
  (skipped because count was 0)

### 3. Dependency Health — {N} finding(s)
  ... detail ...

(etc. for all 6 categories)
```

#### Step 4: Velocity Summary

Always run this step regardless of finding counts — velocity is
informational, not a health issue.

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
2026-W08             3    0.1523
2026-W07             5    0.2100
...
```

If the query returns no rows, display:

```
### Velocity — No completed tasks recorded yet
```

---

### Phase 2: Interactive Q&A

After presenting the audit report, present 5 discussion topics:

1. **Domain alignment** — how well do existing domains match the actual
   work being shipped? Run breakdowns by domain (count, throughput, average
   cost) and propose merges, splits, or renames.
2. **Agent effectiveness** — are agents actually picking up tasks
   assigned to them? Compare assignee → done counts and cost-per-task.
3. **Workflow patterns** — average wall vs active duration, criteria
   coverage rates, review pass counts. Identify common failure modes.
4. **Backlog strategy** — open task age distribution, deferred vs active
   ratio, expiring tasks, dependency depth.
5. **Free-form exploration** — let the user ask any DB question; respond
   with a `tusk -header -column "<sql>"` query and a one-paragraph
   interpretation.

Ask which topic they'd like to explore. For each chosen topic, run the
corresponding queries (using `tusk -header -column` or
`tusk task-list --format json | jq …`), analyze the results, and provide
actionable recommendations.

The user can explore multiple topics or end the session at any time.
