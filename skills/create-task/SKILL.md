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
tusk config complexity
```

Store these for use when assigning metadata. If a field returns an empty list (e.g., domains is `[]`), that field has no validation — use your best judgment or leave it NULL.

## Step 2b: Fetch Existing Backlog

Fetch all open tasks so you can cross-reference proposed tasks against them for semantic overlap:

```bash
tusk -header -column "SELECT id, summary, domain, priority FROM tasks WHERE status <> 'Done' ORDER BY id"
```

Hold these in context for Step 3. The heuristic dupe checker (`tusk dupes check`) catches textually similar tasks, but you can catch **semantic** duplicates that differ in wording — e.g., "Implement password reset flow" vs. existing "Add forgot password endpoint" — which the heuristic would miss.

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
| **complexity** | Estimate effort: `XS` = partial session, `S` = 1 session, `M` = 2-3 sessions, `L` = 3-5 sessions, `XL` = 5+. Default to `M` if unclear. Must be one of the configured complexity values. |

### Decomposition Guidelines

- **One task per deliverable** — if a feature has multiple distinct pieces of work, split them
- **Keep tasks actionable** — each task should be completable in a single focused session
- **Preserve context** — include relevant details from the source text in the description
- **Don't over-split** — trivial sub-steps that are naturally part of a larger task don't need their own row
- **Group related fixes** — multiple closely related bugs can stay as one task if they share a root cause
- **Check for semantic overlap** — compare each proposed task against the existing backlog (from Step 2b). If an existing task covers the same intent with different wording, flag it as a duplicate rather than proposing a new task

## Step 4: Present Task List for Review

Show all proposed tasks in a numbered table before inserting anything:

```markdown
## Proposed Tasks

| # | Summary | Priority | Domain | Type | Complexity | Assignee |
|---|---------|----------|--------|------|------------|----------|
| 1 | Add login endpoint with JWT auth | High | api | feature | M | backend |
| 2 | Add signup page with form validation | Medium | frontend | feature | S | frontend |
| 3 | Fix broken CSS on mobile nav | High | frontend | bug | XS | frontend |
| 4 | Add rate limiting middleware | Medium | api | feature | S | backend |

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

Semantic duplicates should already have been filtered out during Step 3 (by comparing against the backlog from Step 2b). As a deterministic safety net, run a heuristic duplicate check **before** inserting each task:

```bash
tusk dupes check "<summary>"
```

If the domain is set and non-empty, include it:

```bash
tusk dupes check "<summary>" --domain <domain>
```

### Exit code 0 — No duplicate found → Insert the task

Use `tusk sql-quote` to safely escape user-provided text fields. This prevents SQL injection and handles single quotes automatically.

```bash
tusk "INSERT INTO tasks (summary, description, status, priority, domain, task_type, assignee, complexity, created_at, updated_at)
  VALUES (
    $(tusk sql-quote "<summary>"),
    $(tusk sql-quote "<description>"),
    'To Do',
    '<priority>',
    '<domain_or_NULL>',
    '<task_type>',
    '<assignee_or_NULL>',
    '<complexity>',
    datetime('now'),
    datetime('now')
  )"
```

Use `$(tusk sql-quote "...")` for any field that may contain user-provided text (summary, description). Static values from config (priority, domain, task_type, assignee) don't need quoting since they come from validated config values.

For NULL fields, use the literal `NULL` (unquoted) — don't pass it through `sql-quote`:

```bash
tusk "INSERT INTO tasks (summary, description, status, priority, domain, task_type, assignee, complexity, created_at, updated_at)
  VALUES ($(tusk sql-quote "Add rate limiting"), $(tusk sql-quote "Details here"), 'To Do', 'Medium', NULL, 'feature', NULL, 'M', datetime('now'), datetime('now'))"
```

### Exit code 1 — Duplicate found → Skip

Report which existing task matched and skip the insert:

> Skipped "Add login endpoint with JWT auth" — duplicate of existing task #12 (similarity 0.87)

### Exit code 2 — Error

Report the error and skip.

## Step 5b: Generate Acceptance Criteria

For each successfully inserted task, generate **3–7 acceptance criteria** using the task's summary, description, and the original source text that informed it. Criteria should be concrete, testable conditions that define "done" for the task.

### How to derive criteria

- Start from the task's **description** — each distinct requirement or expected behavior maps to a criterion
- Add any implicit quality expectations (e.g., error handling, edge cases, validation) if the task type warrants it
- For **bug** tasks, include a criterion that the specific failure case is resolved
- For **feature** tasks, include criteria for the happy path and at least one edge case
- Keep each criterion to a single sentence — actionable and verifiable

### Insert criteria

For each criterion, run:

```bash
tusk criteria add <task_id> "<criterion text>"
```

Use the task ID returned from the INSERT in Step 5. Example for a task with ID 14:

```bash
tusk criteria add 14 "POST /auth/login returns a JWT token for valid credentials"
tusk criteria add 14 "Invalid credentials return 401 with error message"
tusk criteria add 14 "Refresh token endpoint issues a new JWT"
tusk criteria add 14 "Tokens expire after the configured TTL"
```

### Skip criteria for duplicates

If a task was skipped as a duplicate in Step 5, do not generate criteria for it.

## Step 5c: Propose Dependencies (Multi-Task Only)

If **two or more tasks** were successfully inserted, analyze them for natural ordering and propose dependencies. Skip this step if only one task was created.

### How to identify dependencies

Look for these patterns among the newly created tasks:

- **Schema before code** — a migration or data model task should block feature tasks that depend on it
- **Backend before frontend** — API endpoints often block UI tasks that consume them
- **Core before extension** — foundational tasks (setup, config, base classes) block tasks that build on them
- **Implementation before docs** — documentation tasks should depend on the feature they document
- **Bug fix before feature** — if a new feature depends on a bug being fixed first
- **Contingent relationships** — if task B only makes sense when task A is completed successfully (not just closed), use `contingent` type. Example: "Write integration tests for new API" is contingent on "Build new API endpoint" — if the endpoint is cancelled, the tests are moot

If no natural ordering exists among the created tasks, state that and skip to Step 6.

### Present proposed dependencies

Show a numbered table of proposed dependencies for user approval:

```markdown
## Proposed Dependencies

| # | Task | Depends On | Type | Reason |
|---|------|------------|------|--------|
| 1 | #16 Add signup page | #14 Add auth endpoint | blocks | Frontend consumes the auth API |
| 2 | #17 Write API docs | #14 Add auth endpoint | contingent | Docs are moot if endpoint is cancelled |
```

Then ask:

> Does this look right? You can:
> - **Confirm** to add all dependencies
> - **Remove** specific numbers (e.g., "remove 2")
> - **Change type** (e.g., "change 1 to contingent")
> - **Skip** to add no dependencies

Wait for explicit user approval before inserting.

### Insert approved dependencies

For each approved dependency, run:

```bash
tusk deps add <task_id> <depends_on_id>
```

Or with a specific type:

```bash
tusk deps add <task_id> <depends_on_id> --type contingent
```

Report any validation errors (cycle detected, task not found) and continue with the remaining dependencies.

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
- **Use `tusk sql-quote`** — always wrap user-provided text with `$(tusk sql-quote "...")` in SQL statements
- **Leave `priority_score` at 0** — let `/groom-backlog` compute scores later
- **Use configured values only** — read domains, task_types, agents, and priorities from `tusk config`, never hardcode
- **Adapt to any project** — this skill works with whatever config the target project has
