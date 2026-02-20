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

## Step 2: Fetch Config, Backlog, and Conventions

Fetch everything needed for analysis in a single call:

```bash
tusk setup
```

This returns a JSON object with three keys:
- **`config`** — full project config (domains, task_types, agents, priorities, complexity, etc.). Store for use when assigning metadata. If a field is an empty list (e.g., `"domains": []`), that field has no validation — use your best judgment or leave it NULL.
- **`backlog`** — all open tasks as an array of objects. Hold in context for Step 3. The heuristic dupe checker (`tusk dupes check`) catches textually similar tasks, but you can catch **semantic** duplicates that differ in wording — e.g., "Implement password reset flow" vs. existing "Add forgot password endpoint" — which the heuristic would miss.
- **`conventions`** — learned project heuristics (string, may be empty). If non-empty and contains convention entries (not just the header comment), hold in context as **preamble rules** for Step 3 — they take precedence over the generic decomposition guidelines below. For example, a convention like "bin/tusk-*.py always needs a dispatcher entry in bin/tusk" means a new Python script and its dispatcher line belong in the **same** task, not two separate tickets.

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

### Single-task fast path

If analysis produced **exactly 1 task**, use the compact inline format instead of the full table:

```markdown
## Proposed Task

**Add login endpoint with JWT auth** (High · api · feature · M · backend)
> Implement POST /auth/login that validates credentials and returns a JWT token. Include refresh token support.
```

Then ask:

> Create this task? You can **confirm**, **edit** (e.g., "change priority to Medium"), or **remove** it.

### Multi-task presentation

If analysis produced **2 or more tasks**, show the full numbered table:

```markdown
## Proposed Tasks

| # | Summary | Priority | Domain | Type | Complexity | Assignee |
|---|---------|----------|--------|------|------------|----------|
| 1 | Add login endpoint with JWT auth | High | api | feature | M | backend |
| 2 | Add signup page with form validation | Medium | frontend | feature | S | frontend |

### Details

**1. Add login endpoint with JWT auth**
> Implement POST /auth/login that validates credentials and returns a JWT token. Include refresh token support.

**2. Add signup page with form validation**
> Create signup form with email, password, and confirm password fields. Validate on blur and on submit.
```

Then ask:

> Does this look right? You can:
> - **Confirm** to create all tasks
> - **Remove** specific numbers (e.g., "remove 3")
> - **Edit** a task (e.g., "change 2 priority to High")
> - **Add** a task you think is missing

### For both paths

Wait for explicit user approval before proceeding. Do NOT insert anything until the user confirms.

## Step 5: Deduplicate, Insert, and Generate Criteria

For each approved task, generate **3–7 acceptance criteria** — concrete, testable conditions that define "done." Derive them from the description: each distinct requirement or expected behavior maps to a criterion. For **bug** tasks, include a criterion that the failure case is resolved. For **feature** tasks, include the happy path and at least one edge case.

Then insert the task with criteria in a single call using `tusk task-insert`. This validates enum values against config, runs a heuristic duplicate check internally, and inserts the task + criteria in one transaction:

```bash
tusk task-insert "<summary>" "<description>" \
  --priority "<priority>" \
  --domain "<domain>" \
  --task-type "<task_type>" \
  --assignee "<assignee>" \
  --complexity "<complexity>" \
  --criteria "<criterion 1>" \
  --criteria "<criterion 2>" \
  --criteria "<criterion 3>"
```

For typed criteria with automated verification, use `--typed-criteria` with a JSON object:

```bash
tusk task-insert "<summary>" "<description>" \
  --criteria "Manual criterion" \
  --typed-criteria '{"text":"Tests pass","type":"test","spec":"pytest tests/"}' \
  --typed-criteria '{"text":"Config exists","type":"file","spec":"config/*.json"}'
```

Valid types: `manual` (default), `code`, `test`, `file`. Non-manual types require a `spec` field.

Omit `--domain` or `--assignee` entirely if the value is NULL/empty — do not pass empty strings.

### Exit code 0 — Success

The command prints JSON with `task_id` and `criteria_ids`. Use the `task_id` for dependency proposals in Step 7.

### Exit code 1 — Duplicate found → Skip

The command prints JSON with `matched_task_id` and `similarity`. Report which existing task matched:

> Skipped "Add login endpoint with JWT auth" — duplicate of existing task #12 (similarity 0.87)

### Exit code 2 — Error

Report the error and skip.

## Step 7: Propose Dependencies

Skip this step if:
- Zero tasks were created (all were duplicates), OR
- Exactly **one** task was created (single-task fast path — no inter-task dependencies to propose, and checking against the backlog adds ceremony for the most common use case)

If **two or more** tasks were created, analyze for dependencies. Load the dependency proposal guide:

```
Read file: <base_directory>/DEPENDENCIES.md
```

Then follow its instructions.

## Step 8: Report Results

After processing all tasks, show a summary:

```markdown
## Results

**Created**: 3 tasks (#14, #15, #16)
**Skipped**: 1 duplicate (matched existing #12)
**Dependencies added**: 2 (#16 → #14 (blocks), #17 → #14 (contingent))

| ID | Summary | Priority | Domain |
|----|---------|----------|--------|
| 14 | Add signup page with form validation | Medium | frontend |
| 15 | Fix broken CSS on mobile nav | High | frontend |
| 16 | Add rate limiting middleware | Medium | api |
```

Include the **Dependencies added** line only when Step 7 was executed (i.e., two or more tasks were created). If Step 7 was skipped (all duplicates, single-task fast path, or user skipped all dependencies), omit the line. If dependencies were proposed but the user removed some, only list the ones actually inserted.

### Zero-criteria check

After displaying the summary, verify that every created task has at least one acceptance criterion. For each created task ID, run:

```bash
tusk criteria list <task_id>
```

If any task has **zero criteria**, display a warning:

> **Warning**: Tasks #14, #16 have no acceptance criteria. Go back to Step 6 and generate criteria for them before moving on.

Do not proceed past this step until all created tasks have at least one criterion.

Then, **conditionally** show the updated backlog:

- If **more than 3 tasks were created**, show the full backlog so the user can see where the new tasks landed:

  ```bash
  tusk -header -column "SELECT id, summary, priority, domain, task_type, assignee FROM tasks WHERE status = 'To Do' ORDER BY priority_score DESC, id"
  ```

- If **3 or fewer tasks were created**, show only a count to save tokens:

  ```bash
  tusk "SELECT COUNT(*) || ' open tasks in backlog' FROM tasks WHERE status = 'To Do'"
  ```

## Important Guidelines

- **All DB access goes through `tusk`** — never use raw `sqlite3`
- **Always confirm before inserting** — never insert tasks without explicit user approval
- **Always run dupe checks** — check every task against existing open tasks before inserting
- **Use `tusk sql-quote`** — always wrap user-provided text with `$(tusk sql-quote "...")` in SQL statements
- **Use configured values only** — read domains, task_types, agents, and priorities from `tusk config`, never hardcode
- **Adapt to any project** — this skill works with whatever config the target project has
