---
name: token-audit
description: Analyze skill token consumption and surface optimization opportunities
allowed-tools: Bash, Read
---

# Token Audit

Scans all skill directories and reports token consumption across five categories: size census, companion file analysis, SQL anti-patterns, redundancy detection, and narrative density.

## Step 1: Run the Audit

```bash
tusk token-audit
```

## Step 2: Analyze Findings

Review the report across the **4 actionable categories** below. For each category, read the flagged skills and compose a concrete description of what should change. Skip categories with no actionable findings.

### Companion Files

Look for `UNCONDITIONAL` loads — these inject tokens on every invocation. For each, describe which skill/file needs a conditional guard added.

### SQL Anti-Patterns

Focus on `WARN`-level items (e.g., `SELECT *` pulling unnecessary columns). `INFO` items are advisory — only include them if a clear fix is obvious (e.g., replacing `SELECT *` with named columns).

### Redundancy

Look for `tusk setup` followed by `tusk config`/`tusk conventions` re-fetches in the same skill. Duplicate `tusk` commands across subcommand examples are expected and not actionable.

### Narrative Density

For skills with prose:code ratio > 3.0, read each flagged `SKILL.md` and identify cuttable prose:
- **"Arguments" boilerplate** — "Parse the user's request to determine..." sections the model doesn't need
- **"Integration Points" sections** — lists of which other skills reference this one
- **Explanatory prose that restates the code** — e.g., "What It Shows" sections describing CLI output

Compose a single description per skill listing the specific sections to cut or condense.

## Step 3: Create Tasks

Take the combined findings from Step 2 and pass them as input to `/create-task`. Group related fixes into sensible tasks (e.g., one task per category, or one task for all small skills with the same problem pattern). Let `/create-task` handle decomposition, deduplication, and insertion.
