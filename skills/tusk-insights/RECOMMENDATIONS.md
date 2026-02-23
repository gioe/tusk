# Tusk Insights — Interactive Q&A

Companion file for `/tusk-insights` Phase 2. Contains query templates and analysis prompts for 5 discussion topics.

---

## Topic 1: Domain Alignment

### Queries

**Task distribution by domain:**

```sql
SELECT domain, status, COUNT(*) as count
FROM tasks
WHERE domain IS NOT NULL AND domain <> ''
GROUP BY domain, status
ORDER BY domain, status;
```

**Domains with no open tasks:**

```sql
SELECT DISTINCT domain FROM tasks
WHERE domain IS NOT NULL AND domain <> ''
EXCEPT
SELECT DISTINCT domain FROM tasks
WHERE domain IS NOT NULL AND domain <> '' AND status <> 'Done';
```

**Tasks without a domain:**

```sql
SELECT id, summary, status
FROM tasks
WHERE (domain IS NULL OR domain = '')
  AND status <> 'Done'
ORDER BY id;
```

### Analysis Prompts

- Are any domains overloaded (many open tasks) while others are idle?
- Do the configured domains still match the project's current areas of work?
- Are there tasks that seem miscategorized based on their summary/description?

---

## Topic 2: Agent Effectiveness

### Queries

**Tasks per agent:**

```sql
SELECT assignee, status, COUNT(*) as count
FROM tasks
WHERE assignee IS NOT NULL AND assignee <> ''
GROUP BY assignee, status
ORDER BY assignee, status;
```

**Agent cost and throughput:**

```sql
SELECT t.assignee,
       COUNT(DISTINCT t.id) as tasks_done,
       ROUND(SUM(s.cost_dollars), 2) as total_cost,
       SUM(s.tokens_in + s.tokens_out) as total_tokens,
       ROUND(AVG(s.duration_seconds / 60.0), 1) as avg_session_minutes
FROM tasks t
JOIN task_sessions s ON t.id = s.task_id
WHERE t.assignee IS NOT NULL AND t.assignee <> ''
  AND t.status = 'Done'
GROUP BY t.assignee
ORDER BY tasks_done DESC;
```

**Unassigned open tasks:**

```sql
SELECT id, summary, priority, status
FROM tasks
WHERE (assignee IS NULL OR assignee = '')
  AND status <> 'Done'
ORDER BY priority_score DESC, id;
```

### Analysis Prompts

- Which agents are most/least productive in terms of tasks completed?
- Is cost per task reasonable across agents?
- Are there unassigned tasks that should be routed to a specific agent?

---

## Topic 3: Workflow Patterns

### Queries

**Average time to completion by complexity:**

```sql
SELECT complexity,
       COUNT(*) as completed,
       ROUND(AVG(julianday(updated_at) - julianday(created_at)), 1) as avg_days
FROM tasks
WHERE status = 'Done' AND closed_reason = 'completed'
  AND complexity IS NOT NULL
GROUP BY complexity
ORDER BY
  CASE complexity
    WHEN 'XS' THEN 1 WHEN 'S' THEN 2 WHEN 'M' THEN 3
    WHEN 'L' THEN 4 WHEN 'XL' THEN 5
  END;
```

**Tasks stuck In Progress (> 3 days since last update):**

```sql
SELECT id, summary, complexity, updated_at,
       ROUND(julianday('now') - julianday(updated_at), 1) as days_stale
FROM tasks
WHERE status = 'In Progress'
  AND julianday('now') - julianday(updated_at) > 3
ORDER BY days_stale DESC;
```

**Closed reason breakdown:**

```sql
SELECT closed_reason, COUNT(*) as count,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER(), 1) as pct
FROM tasks
WHERE status = 'Done'
GROUP BY closed_reason
ORDER BY count DESC;
```

**Task creation rate (last 30 days):**

```sql
SELECT date(created_at) as day, COUNT(*) as created
FROM tasks
WHERE created_at >= datetime('now', '-30 days')
GROUP BY day
ORDER BY day;
```

### Analysis Prompts

- Are tasks being completed at a sustainable rate?
- Is complexity estimation accurate (do L/XL tasks actually take longer)?
- What percentage of tasks are closed as wont_do or duplicate (waste indicator)?
- Are there tasks stuck In Progress that need attention?

---

## Topic 4: Backlog Strategy

### Queries

**Backlog size by priority:**

```sql
SELECT priority, COUNT(*) as count
FROM tasks
WHERE status = 'To Do'
GROUP BY priority
ORDER BY
  CASE priority
    WHEN 'Highest' THEN 1 WHEN 'High' THEN 2 WHEN 'Medium' THEN 3
    WHEN 'Low' THEN 4 WHEN 'Lowest' THEN 5
  END;
```

**Oldest open tasks:**

```sql
SELECT id, summary, priority, created_at,
       ROUND(julianday('now') - julianday(created_at), 0) as age_days
FROM tasks
WHERE status = 'To Do'
ORDER BY created_at
LIMIT 10;
```

**Blocked vs ready:**

```bash
tusk deps ready    # tasks with all blockers satisfied
tusk deps blocked  # tasks held up by dependencies or external blockers
```

**Complexity distribution (open tasks):**

```sql
SELECT complexity, COUNT(*) as count
FROM tasks
WHERE status <> 'Done'
GROUP BY complexity
ORDER BY
  CASE complexity
    WHEN 'XS' THEN 1 WHEN 'S' THEN 2 WHEN 'M' THEN 3
    WHEN 'L' THEN 4 WHEN 'XL' THEN 5
  END;
```

### Analysis Prompts

- Is the backlog growing faster than tasks are being completed?
- Are there very old tasks that should be closed or re-prioritized?
- Is the ratio of blocked to ready tasks healthy?
- Does the complexity mix suggest the backlog will take a long time to clear?

---

## Topic 5: Free-Form Exploration

Ask the user what they'd like to explore and build the appropriate read-only `tusk` SQL query. SELECT only; use `tusk -header -column "..."` for formatted output.
