# Full Retrospective (M / L / XL tasks)

Thorough retro for medium-to-large tasks. Includes subsumption analysis, dependency proposals, and detailed reporting.

## Step 1: Review Session History

**Check for custom focus areas first.** Attempt to read `<base_directory>/FOCUS.md`.
- If the file exists: use the categories defined in it for Step 3 instead of the defaults.
- If the file does not exist: use the default categories A–D defined in Step 3.

Analyze the full conversation context. Look for:

- **Friction points** — confusing instructions, missing context, repeated mistakes
- **Workarounds** — manual steps that could be automated or codified into skills
- **Tangential issues** — test failures, tech debt, bugs discovered out of scope
- **Incomplete work** — deferred decisions, TODOs, partial implementations
- **Failed approaches** — strategies that didn't work and why
- **Lint Rules** — concrete, grep-detectable anti-patterns observed in this session (max 3). Only if an actual mistake occurred that a grep rule could prevent.

Review the entire session, not just the most recent messages.

## Step 2: Config, Backlog, and Conventions

Use the JSON already fetched via `tusk setup` in Step 0 of the retro skill: `config` for metadata assignment and `backlog` for semantic duplicate comparison in Step 3.

## Step 3: Categorize Findings

If `<base_directory>/FOCUS.md` was found in Step 1, use those categories.

Otherwise organize into the default four categories:

- **A**: Process improvements — skill/CLAUDE.md/tooling friction, confusing instructions, missing conventions
- **B**: Tangential issues — out-of-scope bugs, tech debt, architectural concerns
- **C**: Follow-up work — incomplete items, deferred decisions, edge cases
- **D**: Lint Rules — concrete, grep-detectable anti-patterns (max 3). Only if an actual mistake occurred that a grep rule could prevent. Filed as tasks, not written directly.

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

### <Category name from Step 3> (N findings)
1. **<title>** — <description>
   → Proposed: <summary> | <priority> | <task_type> | <domain>

(Repeat for each category. Use the resolved category names — from FOCUS.md if present, or defaults A/B/C/D. Omit empty categories.)

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

### 5d: Create Lint Rule Tasks (only if lint rule findings exist)

Apply this step if there are lint rule findings — Category D when using defaults, or a "Lint Rules" section when using a custom FOCUS.md.

For each lint rule finding, create a task whose description contains the exact `tusk lint-rule add` invocation. The retro identifies the pattern and files; the implementing agent runs the command.

The bar is high — only create a lint rule task if you observed an **actual mistake** that a grep rule would have caught. Do not create lint rule tasks for general advice.

```bash
tusk task-insert "Add lint rule: <short description>" \
  "Run: tusk lint-rule add '<pattern>' '<file_glob>' '<message>'" \
  --priority "Low" --task-type "<task_type>" --complexity "XS" \
  --criteria "tusk lint-rule add has been run with the specified pattern, glob, and message"
```

For `<task_type>`: use the project's config `task_types` array (already fetched via `tusk setup` in Step 0). Pick the entry that best fits a maintenance/tooling task (e.g., `maintenance`, `chore`, `tech-debt`, `infra` — whatever is closest in your project's list). If no entry is a clear fit, omit `--task-type` entirely.

Fill in `<pattern>` (grep regex), `<file_glob>` (e.g., `*.md` or `bin/tusk-*.py`), and `<message>` (human-readable warning) with the specific values from your finding.

## Step 6: Report Results

```markdown
## Retrospective Complete

**Session**: <what was accomplished>
**Findings**: N findings by category (use resolved category names)
**Created**: N tasks (#id, #id)
**Lint rule tasks created**: K
**Subsumed**: S findings into existing tasks (#id)
**Dependencies added**: D (if any were created)
**Skipped**: M duplicates
```

Include **Dependencies added** only when Step 5c was executed. Omit if all tasks were duplicates/subsumed.

Then show the backlog:

```bash
tusk -header -column "SELECT id, summary, priority, domain, task_type, status FROM tasks WHERE status = 'To Do' ORDER BY priority_score DESC, id"
```
