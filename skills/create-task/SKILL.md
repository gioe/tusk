---
name: create-task
description: Break down freeform text (feature specs, meeting notes, bug reports) into structured tusk tasks with deduplication
allowed-tools: Bash, Read
---

# Create Task Skill

Takes arbitrary text input — feature specs, meeting notes, brainstorm lists, bug reports, requirements docs — and decomposes it into structured, deduplicated tasks in the tusk database.

## Step 1: Capture Input

The user provides freeform text after `/create-task`. This could be:
- A feature description or requirements list
- Meeting notes or brainstorm output
- A bug report or incident summary
- A pasted document or spec
- A simple one-liner for a single task

If the user didn't provide any text after the command, ask:

> What would you like to turn into tasks? Paste any text — feature specs, meeting notes, bug reports, requirements, etc.

## Step 2: Read Project Config

Fetch valid values so tasks conform to the project's configured constraints:

```bash
tusk config domains
tusk config task_types
tusk config agents
tusk config priorities
```

Store these for use when assigning metadata. If a field returns an empty list (e.g., domains is `[]`), that field has no validation — use your best judgment or leave it NULL.

## Step 3: Analyze and Decompose

Break the input into discrete, actionable tasks. For each task, determine:

| Field | How to Determine |
|-------|-----------------|
| **summary** | Clear, imperative sentence describing the deliverable (e.g., "Add login endpoint with JWT authentication"). Max ~100 chars. |
| **description** | Expanded context from the input — acceptance criteria, technical notes, relevant quotes from the source text. |
| **priority** | Infer from language cues: "critical"/"urgent"/"blocking" → `Highest`/`High`; "nice to have"/"eventually" → `Low`/`Lowest`; default to `Medium`. Must be one of the configured priorities. |
| **domain** | Match to a configured domain based on the task's subject area. Leave NULL if no domains are configured or none fit. |
| **task_type** | Categorize as one of the configured task types (bug, feature, refactor, etc.). Default to `feature` for new work, `bug` for fixes. |
| **assignee** | Match to a configured agent if the task clearly falls in their area. Leave NULL if unsure. |

### Decomposition Guidelines

- **One task per deliverable** — if a feature has multiple distinct pieces of work, split them
- **Keep tasks actionable** — each task should be completable in a single focused session
- **Preserve context** — include relevant details from the source text in the description
- **Don't over-split** — trivial sub-steps that are naturally part of a larger task don't need their own row
- **Group related fixes** — multiple closely related bugs can stay as one task if they share a root cause

## Step 4: Present Task List for Review

Show all proposed tasks in a numbered table before inserting anything:

```markdown
## Proposed Tasks

| # | Summary | Priority | Domain | Type | Assignee |
|---|---------|----------|--------|------|----------|
| 1 | Add login endpoint with JWT auth | High | api | feature | backend |
| 2 | Add signup page with form validation | Medium | frontend | feature | frontend |
| 3 | Fix broken CSS on mobile nav | High | frontend | bug | frontend |
| 4 | Add rate limiting middleware | Medium | api | feature | backend |

### Details

**1. Add login endpoint with JWT auth**
> Implement POST /auth/login that validates credentials and returns a JWT token. Include refresh token support.

**2. Add signup page with form validation**
> Create signup form with email, password, and confirm password fields. Validate on blur and on submit.

...
```

Then ask:

> Does this look right? You can:
> - **Confirm** to create all tasks
> - **Remove** specific numbers (e.g., "remove 3")
> - **Edit** a task (e.g., "change 2 priority to High")
> - **Add** a task you think is missing

Wait for explicit user approval before proceeding. Do NOT insert anything until the user confirms.

## Step 5: Deduplicate and Insert

For each approved task, run a duplicate check **before** inserting:

```bash
tusk dupes check "<summary>"
```

If the domain is set and non-empty, include it:

```bash
tusk dupes check "<summary>" --domain <domain>
```

### Exit code 0 — No duplicate found → Insert the task

**IMPORTANT**: Escape single quotes in all text fields by doubling them (`'` → `''`) to prevent SQL errors.

```bash
tusk "INSERT INTO tasks (summary, description, status, priority, domain, task_type, assignee, created_at, updated_at)
  VALUES (
    '<summary>',
    '<description>',
    'To Do',
    '<priority>',
    '<domain_or_NULL>',
    '<task_type>',
    '<assignee_or_NULL>',
    datetime('now'),
    datetime('now')
  )"
```

For NULL fields, use the literal `NULL` (unquoted) instead of a quoted string:

```bash
tusk "INSERT INTO tasks (summary, description, status, priority, domain, task_type, assignee, created_at, updated_at)
  VALUES ('Add rate limiting', 'Details here', 'To Do', 'Medium', NULL, 'feature', NULL, datetime('now'), datetime('now'))"
```

### Exit code 1 — Duplicate found → Skip

Report which existing task matched and skip the insert:

> Skipped "Add login endpoint with JWT auth" — duplicate of existing task #12 (similarity 0.87)

### Exit code 2 — Error

Report the error and skip.

## Step 6: Report Results

After processing all tasks, show a summary:

```markdown
## Results

**Created**: 3 tasks (#14, #15, #16)
**Skipped**: 1 duplicate (matched existing #12)

| ID | Summary | Priority | Domain |
|----|---------|----------|--------|
| 14 | Add signup page with form validation | Medium | frontend |
| 15 | Fix broken CSS on mobile nav | High | frontend |
| 16 | Add rate limiting middleware | Medium | api |
```

Then show the final state:

```bash
tusk -header -column "SELECT id, summary, priority, domain, task_type, assignee FROM tasks WHERE status = 'To Do' ORDER BY priority_score DESC, id"
```

## Important Guidelines

- **All DB access goes through `tusk`** — never use raw `sqlite3`
- **Always confirm before inserting** — never insert tasks without explicit user approval
- **Always run dupe checks** — check every task against existing open tasks before inserting
- **Escape single quotes** — replace `'` with `''` in all SQL string values
- **Leave `priority_score` at 0** — let `/groom-backlog` compute scores later
- **Use configured values only** — read domains, task_types, agents, and priorities from `tusk config`, never hardcode
- **Adapt to any project** — this skill works with whatever config the target project has
