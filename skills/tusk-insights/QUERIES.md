# Tusk Insights — Detail Queries

Companion file for `/tusk-insights`. Contains detail SQL queries for all 6 audit categories. Only load this file for categories with findings > 0 in the pre-check.

---

## 1. Config Fitness

These queries find tasks with values that don't match the current config. **Skip any query where the corresponding config array is empty** (empty = no validation = no orphans).

### Orphaned Domains

Only run if `config.domains` is non-empty:

```sql
SELECT id, summary, domain
FROM tasks
WHERE status <> 'Done'
  AND domain IS NOT NULL AND domain <> ''
  AND domain NOT IN ({DOMAIN_LIST})
ORDER BY domain, id;
```

Replace `{DOMAIN_LIST}` with quoted, comma-separated values from `config.domains`.

### Orphaned Agents

Only run if `config.agents` is non-empty:

```sql
SELECT id, summary, assignee
FROM tasks
WHERE status <> 'Done'
  AND assignee IS NOT NULL AND assignee <> ''
  AND assignee NOT IN ({AGENT_LIST})
ORDER BY assignee, id;
```

Replace `{AGENT_LIST}` with quoted, comma-separated keys from `config.agents`.

### Orphaned Task Types

```sql
SELECT id, summary, task_type
FROM tasks
WHERE status <> 'Done'
  AND task_type IS NOT NULL AND task_type <> ''
  AND task_type NOT IN ({TASK_TYPE_LIST})
ORDER BY task_type, id;
```

Replace `{TASK_TYPE_LIST}` with quoted, comma-separated values from `config.task_types`.

---

## 2. Task Hygiene

### Done Without Closed Reason

```sql
SELECT id, summary, updated_at
FROM tasks
WHERE status = 'Done'
  AND (closed_reason IS NULL OR closed_reason = '')
ORDER BY updated_at DESC;
```

### In Progress Without Session

```sql
SELECT id, summary, updated_at
FROM tasks
WHERE status = 'In Progress'
  AND id NOT IN (SELECT DISTINCT task_id FROM task_sessions)
ORDER BY id;
```

### Expired But Still Open

```sql
SELECT id, summary, status, expires_at
FROM tasks
WHERE expires_at IS NOT NULL
  AND expires_at < datetime('now')
  AND status <> 'Done'
ORDER BY expires_at;
```

### Missing Description

```sql
SELECT id, summary, status
FROM tasks
WHERE (description IS NULL OR description = '')
  AND status <> 'Done'
ORDER BY id;
```

### Missing Complexity

```sql
SELECT id, summary, status
FROM tasks
WHERE (complexity IS NULL OR complexity = '')
  AND status <> 'Done'
ORDER BY id;
```

---

## 3. Dependency Health

### Blocked by Dead-End Closures

Tasks that depend on a blocker closed as `wont_do` or `duplicate` — the dependency will never meaningfully resolve.

```sql
SELECT d.task_id, t.summary as blocked_task, d.depends_on_id,
       b.summary as blocker_summary, b.closed_reason
FROM task_dependencies d
JOIN tasks t ON d.task_id = t.id
JOIN tasks b ON d.depends_on_id = b.id
WHERE t.status <> 'Done'
  AND b.status = 'Done'
  AND b.closed_reason IN ('wont_do', 'duplicate')
ORDER BY d.task_id;
```

### Dependency Chain Depth

Tasks with deep chains of unsatisfied blockers (depth > 2):

```sql
WITH RECURSIVE dep_chain(root_task, current_task, depth) AS (
  SELECT d.task_id, d.depends_on_id, 1
  FROM task_dependencies d
  JOIN tasks blocker ON d.depends_on_id = blocker.id
  WHERE blocker.status <> 'Done'
    AND d.relationship_type = 'blocks'
  UNION ALL
  SELECT dc.root_task, d.depends_on_id, dc.depth + 1
  FROM dep_chain dc
  JOIN task_dependencies d ON d.task_id = dc.current_task
  JOIN tasks blocker ON d.depends_on_id = blocker.id
  WHERE blocker.status <> 'Done'
    AND d.relationship_type = 'blocks'
    AND dc.depth < 10
)
SELECT dc.root_task as task_id, t.summary, MAX(dc.depth) as chain_depth
FROM dep_chain dc
JOIN tasks t ON dc.root_task = t.id
WHERE t.status <> 'Done'
GROUP BY dc.root_task
HAVING MAX(dc.depth) > 2
ORDER BY chain_depth DESC;
```

---

## 4. Session / Metrics Gaps

### Unclosed Sessions

```sql
SELECT s.id as session_id, s.task_id, t.summary, s.started_at
FROM task_sessions s
JOIN tasks t ON s.task_id = t.id
WHERE s.ended_at IS NULL
ORDER BY s.started_at;
```

### Done Tasks Without Sessions

```sql
SELECT id, summary, updated_at
FROM tasks
WHERE status = 'Done'
  AND id NOT IN (SELECT DISTINCT task_id FROM task_sessions)
ORDER BY updated_at DESC;
```

### Sessions Missing Cost Data

```sql
SELECT s.id as session_id, s.task_id, t.summary,
       s.cost_dollars, s.tokens_in, s.tokens_out
FROM task_sessions s
JOIN tasks t ON s.task_id = t.id
WHERE s.ended_at IS NOT NULL
  AND (s.cost_dollars IS NULL OR s.tokens_in IS NULL OR s.tokens_out IS NULL)
ORDER BY s.id;
```

---

## 5. Acceptance Criteria

### Active Tasks Without Criteria

```sql
SELECT id, summary, status
FROM tasks
WHERE status IN ('In Progress', 'Done')
  AND id NOT IN (SELECT DISTINCT task_id FROM acceptance_criteria)
ORDER BY status, id;
```

### Done Tasks With Incomplete Criteria

```sql
SELECT t.id, t.summary,
       COUNT(ac.id) as total_criteria,
       SUM(CASE WHEN ac.is_completed = 0 THEN 1 ELSE 0 END) as incomplete
FROM tasks t
JOIN acceptance_criteria ac ON t.id = ac.task_id
WHERE t.status = 'Done'
GROUP BY t.id
HAVING incomplete > 0
ORDER BY incomplete DESC;
```

### All Criteria Done But Task Not Done

```sql
SELECT t.id, t.summary, t.status,
       COUNT(ac.id) as total_criteria
FROM tasks t
JOIN acceptance_criteria ac ON t.id = ac.task_id
WHERE t.status <> 'Done'
GROUP BY t.id
HAVING SUM(CASE WHEN ac.is_completed = 0 THEN 1 ELSE 0 END) = 0
ORDER BY t.id;
```

---

## 6. Priority Scoring

### Unscored To Do Tasks

```sql
SELECT id, summary, priority, complexity
FROM tasks
WHERE status = 'To Do'
  AND (priority_score IS NULL OR priority_score = 0)
ORDER BY priority, id;
```

### Score Distribution

```sql
SELECT
  CASE
    WHEN priority_score >= 20 THEN '20+  (very high)'
    WHEN priority_score >= 10 THEN '10-19 (high)'
    WHEN priority_score >= 5  THEN '5-9   (medium)'
    WHEN priority_score > 0   THEN '1-4   (low)'
    ELSE '0     (unscored)'
  END as score_range,
  COUNT(*) as task_count
FROM tasks
WHERE status = 'To Do'
GROUP BY score_range
ORDER BY MIN(priority_score) DESC;
```
