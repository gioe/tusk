---
name: tusk-insights
description: Read-only DB health audit with interactive recommendations
allowed-tools: Bash, Read
---

# Tusk Insights

Read-only health audit of the task database followed by interactive recommendations.

**Phase 1** runs a non-interactive audit across 6 categories, presenting findings as a structured report.
**Phase 2** opens an interactive Q&A session for deeper exploration.

---

## Phase 1: Audit

### Step 1: Load Config

```bash
tusk config
```

Parse the JSON. Note which arrays are empty — empty means no validation is configured for that column:

- `domains` → `[]` means skip domain orphan checks
- `agents` → `{}` means skip agent orphan checks

Hold onto the config values for Step 2.

### Step 2: Pre-Check Counts

Run a **single query** to count findings per category. Build it by substituting config values into the template below.

**Config fitness sub-expression:** Build this dynamically from the config loaded in Step 1.

For each **non-empty** config array, add a condition checking open tasks for non-matching values:

- `domains` (if not `[]`): `(domain IS NOT NULL AND domain <> '' AND domain NOT IN ('d1','d2',...))`
- `agents` keys (if not `{}`): `(assignee IS NOT NULL AND assignee <> '' AND assignee NOT IN ('a1','a2',...))`
- `task_types`: `(task_type IS NOT NULL AND task_type NOT IN ('t1','t2',...))`

Combine conditions with `OR` inside a subselect counting open tasks (`status <> 'Done'`). If **all** config arrays that could produce orphans are empty, use `0` as the config_fitness value.

**Full pre-check template** (replace `{CONFIG_FITNESS_EXPR}` with the expression built above):

```sql
SELECT
  {CONFIG_FITNESS_EXPR} as config_fitness,

  (SELECT COUNT(*) FROM tasks WHERE
    (status = 'Done' AND (closed_reason IS NULL OR closed_reason = ''))
    OR (status = 'In Progress' AND id NOT IN (SELECT DISTINCT task_id FROM task_sessions))
    OR (expires_at IS NOT NULL AND expires_at < datetime('now') AND status <> 'Done')
    OR (description IS NULL OR description = '')
    OR (complexity IS NULL OR complexity = '')
  ) as task_hygiene,

  (SELECT COUNT(DISTINCT d.task_id) FROM task_dependencies d
    JOIN tasks dep ON d.task_id = dep.id
    JOIN tasks blocker ON d.depends_on_id = blocker.id
    WHERE dep.status <> 'Done'
      AND blocker.status = 'Done'
      AND blocker.closed_reason IN ('wont_do', 'duplicate')
  ) as dependency_health,

  (SELECT COUNT(*) FROM task_sessions WHERE ended_at IS NULL)
  + (SELECT COUNT(*) FROM tasks WHERE status = 'Done'
      AND id NOT IN (SELECT DISTINCT task_id FROM task_sessions))
  as session_gaps,

  (SELECT COUNT(*) FROM tasks WHERE status IN ('In Progress', 'Done')
    AND id NOT IN (SELECT DISTINCT task_id FROM acceptance_criteria))
  + (SELECT COUNT(*) FROM tasks WHERE status = 'Done'
    AND id IN (SELECT task_id FROM acceptance_criteria WHERE is_completed = 0))
  as criteria_gaps,

  (SELECT COUNT(*) FROM tasks
    WHERE status = 'To Do'
    AND (priority_score IS NULL OR priority_score = 0))
  as scoring_gaps;
```

Run with `tusk -header -column "..."`.

### Step 3: Audit Report

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

---

## Phase 2: Interactive Q&A

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
