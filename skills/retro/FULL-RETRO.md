# Full Retrospective (M / L / XL tasks)

Thorough retro for medium-to-large tasks. Includes subsumption analysis, dependency proposals, and detailed reporting.

## Step 1: Review Session History

Analyze the full conversation context. Look for:

- **Friction points** — confusing instructions, missing context, repeated mistakes
- **Workarounds** — manual steps that could be automated or codified into skills
- **Tangential issues** — test failures, tech debt, bugs discovered out of scope
- **Incomplete work** — deferred decisions, TODOs, partial implementations
- **Failed approaches** — strategies that didn't work and why
- **Conventions** — generalizable heuristics: file coupling patterns, decomposition rules, naming conventions, workflow patterns that recur across sessions

Review the entire session, not just the most recent messages.

## Step 2: Config, Backlog, and Conventions

Use the JSON already fetched via `tusk setup` in Step 0 of the retro skill: `config` for metadata assignment, `backlog` for semantic duplicate comparison in Step 3, and `conventions` for Step 5d.

## Step 3: Categorize Findings

Organize into four categories:

- **A**: Process improvements — skill/CLAUDE.md/tooling friction, confusing instructions, missing conventions
- **B**: Tangential issues — out-of-scope bugs, tech debt, architectural concerns
- **C**: Follow-up work — incomplete items, deferred decisions, edge cases
- **D**: Conventions — generalizable heuristics (file coupling, decomposition rules, naming). Written to DB via `tusk conventions add`, not filed as tasks.

If a category has no findings, note that explicitly — an empty category is a positive signal.

### 3b: Pre-filter Duplicates

Semantic duplicates should already be filtered by comparing against the backlog above. As a safety net, run heuristic checks:

```bash
tusk dupes check "<proposed summary>"
# Include --domain if set:
tusk dupes check "<proposed summary>" --domain <domain>
```

- Exit 0: keep the finding.
- Exit 1: remove it — record the match (existing task ID, similarity score) for the report.
- Exit 2 (error): keep the finding, let Step 5 handle it.

### 3c: Subsumption Check

For each finding that passed dupe check, evaluate whether it should be folded into an existing task rather than filed separately.

**Criteria** (two or more → recommend subsumption):
- Same file/module affected
- A single PR would address both items
- Small relative scope vs. existing task
- Same domain and goal

For each subsumed finding, record: the existing task ID and a proposed description amendment.

## Step 4: Present Report

Show all findings in a structured report:

```markdown
## Session Retrospective

### Summary
Brief (2-3 sentence) overview of what the session accomplished.

### Category A: Process Improvements (N findings)
1. **<title>** — <description>
   → Proposed: <summary> | <priority> | <task_type> | <domain>

### Category B: Tangential Issues (N findings)
1. **<title>** — <description>
   → Proposed: <summary> | <priority> | <task_type> | <domain>

### Category C: Follow-up Work (N findings)
1. **<title>** — <description>
   → Proposed: <summary> | <priority> | <task_type> | <domain>

### Category D: Conventions (N findings) (omit if none)
1. **<short title>** — <description of the heuristic>

### Duplicates Already Tracked (omit if none)
| Finding | Matched Task | Similarity |
|---------|-------------|------------|

### Subsumed into Existing Tasks (omit if none)
| Finding | Merge Into | Reason | Proposed Amendment |
|---------|-----------|--------|-------------------|

### Proposed Tasks (new work only)
| # | Summary | Priority | Domain | Type | Category |
|---|---------|----------|--------|------|----------|
```

Then ask the user to **confirm**, **remove** specific numbers, **edit** a task, **reject subsumption**, **add** a finding, or **skip**. Wait for explicit approval before inserting.

## Step 5: Apply Approved Changes

### 5a: Apply Subsumptions

```bash
EXISTING_DESC=$(tusk "SELECT description FROM tasks WHERE id = <id>")
AMENDED_DESC="${EXISTING_DESC}

---
Subsumed from retro finding: <finding summary>
<amendment text>"
tusk "UPDATE tasks SET description = $(tusk sql-quote "$AMENDED_DESC"), updated_at = datetime('now') WHERE id = <id>"
```

### 5b: Insert New Tasks

```bash
tusk task-insert "<summary>" "<description>" --priority "<priority>" --domain "<domain>" --task-type "<task_type>" --assignee "<assignee>" --complexity "<complexity>" \
  --criteria "<criterion 1>" [--criteria "<criterion 2>" ...]
```

Always include at least one `--criteria` flag — derive 1–3 concrete acceptance criteria from the task description. Omit `--domain` or `--assignee` entirely if the value is NULL/empty. Exit code 1 means duplicate — skip.

### 5c: Propose Dependencies

Skip if zero tasks were created. For one or more new tasks, check for ordering constraints — both among new tasks and against the existing backlog. Only propose when there's a clear reason one must complete before another can begin.

**Common patterns:** process change before feature, bug fix before follow-up, schema/infra before code, new task extends existing backlog task.

Present a numbered table for approval:

| # | Task | Depends On | Type | Reason |
|---|------|------------|------|--------|

Then insert approved dependencies with `tusk deps add <task_id> <depends_on_id> [--type contingent]`.

### 5d: Write Conventions (only if Category D has findings)

Check the `conventions` string from Step 0 (passed through Step 2) to avoid duplicates.

Skip any convention whose meaning is already captured (even if worded differently). For each new convention, insert it into the DB:

```bash
CONV_TEXT=$(cat << 'CONVEOF'
## <short title>

<one-to-two sentence description of the convention and when it applies>
CONVEOF
)
tusk conventions add "$CONV_TEXT" --source retro
```

The DB records `created_at` automatically. Do not append to `tusk/conventions.md` — the DB is now the source of truth.

## Step 6: Report Results

```markdown
## Retrospective Complete

**Session**: <what was accomplished>
**Findings**: A process / B tangential / C follow-up / D conventions
**Created**: N tasks (#id, #id)
**Conventions written**: K new (L skipped as duplicates)
**Subsumed**: S findings into existing tasks (#id)
**Dependencies added**: D (if any were created)
**Skipped**: M duplicates
```

Include **Dependencies added** only when Step 5c was executed. Omit if all tasks were duplicates/subsumed.

Then show the backlog:

```bash
tusk -header -column "SELECT id, summary, priority, domain, task_type, status FROM tasks WHERE status = 'To Do' ORDER BY priority_score DESC, id"
```
