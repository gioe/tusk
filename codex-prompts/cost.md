# Cost (Codex)

Return the requested cost from Tusk's canonical read-only rollups. Do not query
SQLite directly, recompute totals, or mutate/backfill cost data.

Resolve the request as follows:

- `TASK-N`, or an integer explicitly described as a task ID: run
  `tusk task-summary <TASK_ID> --format json` and read `cost.total`.
- `OBJ-N`, or an integer explicitly described as an objective ID: run
  `tusk objective brief <OBJECTIVE_ID> --format json` and read
  `cost.total_cost_dollars`.
- No identifier, `project`, `complete project`, or `entire project`: run
  `tusk cost --format json` and read `total_cost_dollars`.
- A bare integer without task/objective context is ambiguous; ask which scope
  the user means because task and objective IDs can overlap.

Return one concise line with the normalized identifier and cost formatted to
four decimal places:

```text
TASK-123 cost: $1.2345
OBJ-4 cost: $12.3456 across 7 tasks
Project cost: $123.4567
```

For project scope, append a short coverage warning when either
`coverage.task_sessions_missing_cost` or `coverage.skill_runs_missing_cost` is
nonzero; the reported total excludes rows without recorded cost. If Tusk
rejects an identifier, surface that error and do not guess another scope.
