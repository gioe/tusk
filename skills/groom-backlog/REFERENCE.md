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

After applying complexity estimates, recompute priority scores for all open tasks:

```bash
tusk wsjf
```

See CLAUDE.md Key Conventions for the WSJF formula details.
