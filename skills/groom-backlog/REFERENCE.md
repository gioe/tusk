# Groom Backlog Reference: Complexity Sizing & WSJF Scoring

## Step 6b: Estimate Complexity

For each unsized task, estimate complexity using this scale:

| Size | Meaning |
|------|---------|
| `XS` | Partial session — a quick tweak or config change |
| `S`  | ~1 session — a focused, well-scoped change |
| `M`  | 2–3 sessions — moderate scope, may touch several files |
| `L`  | 3–5 sessions — significant feature or cross-cutting change |
| `XL` | 5+ sessions — large effort, architectural change |

Base the estimate on the task's summary, description, and domain. When unsure, default to `M`.

## Step 6c: Present Estimates for Approval

Show all proposed estimates in a table:

```markdown
| ID | Summary | Estimated Complexity |
|----|---------|---------------------|
| 12 | Add rate limiting middleware | S |
| 17 | Refactor auth module | L |
```

Ask the user to confirm or adjust before applying.

## Step 6d: Apply Estimates

After approval, update each task:

```bash
tusk task-update <id> --complexity "<size>"
```

## Step 7: Compute Priority Scores (WSJF)

After all grooming changes are complete (including complexity estimates from Step 6), compute `priority_score` for all open tasks using WSJF (Weighted Shortest Job First) scoring.

### Scoring Formula

```
raw_score = base_priority + source_bonus + unblocks_bonus + contingent_penalty
priority_score = ROUND(raw_score / complexity_weight)
```

Where:
- **base_priority**: Highest=100, High=80, Medium=60, Low=40, Lowest=20
- **source_bonus**: +10 if NOT a `[Deferred]` task
- **unblocks_bonus**: +5 per dependent task, capped at +15
- **contingent_penalty**: -10 if ALL of the task's blockers are `contingent` (none are `blocks`), biasing `/next-task` toward tasks with guaranteed value
- **complexity_weight**: XS=1, S=2, M=3, L=5, XL=8 (NULL defaults to 3, same as M)

This gives small, high-priority tasks a natural advantage over large ones — a High/XS task (90) outranks a Highest/XL task (14).

```bash
tusk "
UPDATE tasks SET priority_score = ROUND(
  (
    CASE priority
      WHEN 'Highest' THEN 100
      WHEN 'High' THEN 80
      WHEN 'Medium' THEN 60
      WHEN 'Low' THEN 40
      WHEN 'Lowest' THEN 20
      ELSE 40
    END
    + CASE WHEN summary NOT LIKE '%[Deferred]%' THEN 10 ELSE 0 END
    + MIN(COALESCE((
      SELECT COUNT(*) * 5
      FROM task_dependencies d
      WHERE d.depends_on_id = tasks.id
    ), 0), 15)
    + CASE WHEN EXISTS (
      SELECT 1 FROM task_dependencies d
      WHERE d.task_id = tasks.id AND d.relationship_type = 'contingent'
    ) AND NOT EXISTS (
      SELECT 1 FROM task_dependencies d
      WHERE d.task_id = tasks.id AND d.relationship_type = 'blocks'
    ) THEN -10 ELSE 0 END
  ) * 1.0
  / CASE complexity
      WHEN 'XS' THEN 1
      WHEN 'S' THEN 2
      WHEN 'M' THEN 3
      WHEN 'L' THEN 5
      WHEN 'XL' THEN 8
      ELSE 3
    END
)
WHERE status <> 'Done';
"
```
