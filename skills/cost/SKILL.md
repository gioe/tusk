---
name: cost
description: Report the recorded cost of a Tusk task, objective, or complete project. Use when the user invokes /cost or asks how much a TASK-N, OBJ-N, named task/objective identifier, or the project as a whole has cost.
allowed-tools: Bash
---

# Cost

Return the requested cost from Tusk's canonical read-only rollups. Do not query
SQLite directly, recompute totals, or mutate/backfill cost data.

## Resolve the scope

- Treat `TASK-N` or an integer explicitly described as a task ID as a task.
- Treat `OBJ-N` or an integer explicitly described as an objective ID as an
  objective.
- Treat no identifier, `project`, `complete project`, or `entire project` as
  the project.
- If the user supplies only a bare integer with no task/objective context, ask
  which scope they mean because task and objective IDs can overlap.

Run exactly one command for the resolved scope:

```bash
# Task
tusk task-summary <TASK_ID> --format json

# Objective
tusk objective brief <OBJECTIVE_ID> --format json

# Complete project
tusk cost --format json
```

Read the cost from:

- Task: `cost.total`
- Objective: `cost.total_cost_dollars`
- Project: `total_cost_dollars`

## Report

Return one concise line with the normalized identifier and cost formatted to
four decimal places, for example:

```text
TASK-123 cost: $1.2345
OBJ-4 cost: $12.3456 across 7 tasks
Project cost: $123.4567
```

For project scope, append a short coverage warning when either
`coverage.task_sessions_missing_cost` or `coverage.skill_runs_missing_cost` is
nonzero; the reported total excludes rows without recorded cost. If Tusk
rejects an identifier, surface that error and do not guess another scope.
