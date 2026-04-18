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

### Deferred Mode Detection

Check whether deferred insertion was requested before proceeding:
- **Caller flag**: The invocation includes `--deferred` (e.g., `/create-task --deferred <text>`)
- **Inline request**: The input text contains an explicit deferred intent phrase such as "add as deferred", "add these as deferred", "insert as deferred", or "create as deferred"

If either condition is met, set **deferred mode = on** and strip the `--deferred` flag (if present) from the input text before proceeding. Do not ask the user to confirm deferred mode — it was explicitly requested.

If neither condition is met, **deferred mode = off** and all tasks are inserted as active (existing behavior, no change).

## Step 2: Fetch Config and Backlog

Fetch everything needed for analysis in a single call:

```bash
tusk setup
```

This returns a JSON object with two keys:
- **`config`** — full project config (domains, task_types, agents, priorities, complexity, etc.). Store for use when assigning metadata. If a field is an empty list (e.g., `"domains": []`), that field has no validation — use your best judgment or leave it NULL.
- **`backlog`** — all open tasks as an array of objects. Hold in context for Step 3. The heuristic dupe checker (`tusk dupes check`) catches textually similar tasks, but you can catch **semantic** duplicates that differ in wording — e.g., "Implement password reset flow" vs. existing "Add forgot password endpoint" — which the heuristic would miss.

## Step 3: Analyze and Decompose

Break the input into discrete, actionable tasks. For each task, determine:

| Field | How to Determine |
|-------|-----------------|
| **summary** | Clear, imperative sentence describing the deliverable (e.g., "Add login endpoint with JWT authentication"). Max ~100 chars. |
| **description** | Expanded context from the input — acceptance criteria, technical notes, relevant quotes from the source text. |
| **priority** | Infer from language cues: "critical"/"urgent"/"blocking" → `Highest`/`High`; "nice to have"/"eventually" → `Low`/`Lowest`; default to `Medium`. Must be one of the configured priorities. |
| **domain** | Match to a configured domain based on the task's subject area. Leave NULL if no domains are configured or none fit. |
| **task_type** | Categorize as one of the configured task types (bug, feature, refactor, test, docs, infrastructure). Default to `feature` for new work, `bug` for fixes. For `test` and `docs`: use as `task_type` only when writing tests or docs **is the primary deliverable** — otherwise use acceptance criteria. See **Task Type Decision Guide** below. |
| **assignee** | Match to a configured agent if the task clearly falls in their area. Leave NULL if unsure. |
| **complexity** | Estimate effort: `XS` = partial session, `S` = 1 session, `M` = 2-3 sessions, `L` = 3-5 sessions, `XL` = 5+. Default to `M` if unclear. Must be one of the configured complexity values. |

### Task Type Decision Guide

The key question: **Is this type the primary deliverable, or is it proof that another deliverable is done?**

| Task Type | Use as `task_type` when the work *is* this | Use as acceptance criterion when this *verifies* other work |
|-----------|---------------------------------------------|-------------------------------------------------------------|
| **bug** | The deliverable is fixing a defect — "Fix login crash on empty password" | A regression must not recur — "Empty password no longer crashes" |
| **feature** | The deliverable is new functionality | N/A — features are always tasks, never criteria |
| **refactor** | The deliverable is restructuring code without changing behavior | N/A — refactoring is always a primary deliverable, never just verification |
| **test** | Writing tests **is the goal** — "Write test suite for auth module" | Tests verify a feature is done — "All auth endpoints have passing tests" |
| **docs** | Writing docs **is the goal** — "Write v2→v3 migration guide" | Docs confirm completion — "API endpoint is documented in README" |
| **infrastructure** | The deliverable is tooling, CI, or infra changes | N/A — infra work is always a task |

**Key rule:** If removing the work would leave the *feature itself* incomplete → use as `task_type`. If removing it just removes *verification* of an already-complete feature → use as an acceptance criterion.

### Decomposition Guidelines

- **One task per deliverable** — if a feature has multiple distinct pieces of work, split them
- **Keep tasks actionable** — each task should be completable in a single focused session
- **Preserve context** — include relevant details from the source text in the description
- **Don't over-split** — trivial sub-steps that are naturally part of a larger task don't need their own row
- **Group related fixes** — multiple closely related bugs can stay as one task if they share a root cause
- **Check for semantic overlap** — compare each proposed task against the existing backlog (from Step 2b). If an existing task covers the same intent with different wording, flag it as a duplicate rather than proposing a new task

## Step 3.5: Pre-Verify Bug Test Failures

**Run this step only when the input describes a bug that claims a specific test is failing or references a pre-existing test failure.** Skip for all other input types.

Trigger signals (any one is sufficient):
- The input uses phrases like "pre-existing failing test", "test is failing", "failing test", "test fails", or names a specific test file or test function alongside a bug description
- The `task_type` determined in Step 3 is `bug` **and** the description references a test by name or path

If triggered:

1. **Detect the test command:**
   ```bash
   tusk test-detect
   ```
   If `confidence` is `"none"`, skip the rest of this step — no test runner could be identified.

2. **Run the referenced test.** Extract the test name, file, or pattern from the input and run it against the detected command. For example, if the test command is `pytest` and the input mentions `test_foo_bar`, run:
   ```bash
   <test_command> <test_reference>   # e.g. pytest tests/unit/test_foo.py::test_foo_bar
   ```
   If no specific test name or file can be identified from the input, skip the rest of this step — treat as indeterminate.
   Limit to 60 seconds. If the command times out or errors for reasons unrelated to test failure (e.g. import error, missing dependency), skip the rest of this step — treat as indeterminate.

3. **Evaluate the result:**
   - **Test fails (non-zero exit):** Failure confirmed. Proceed to Step 4 without comment — the bug is real.
   - **Test passes (exit 0):** Surface this before presenting the proposal:

     > **Pre-verification note:** The referenced test is currently **passing** on this branch — the failure described may not be pre-existing. Do you still want to create a bug task for it?

     Wait for the user's response. If they say no or cancel, stop. If they confirm, proceed to Step 4 with the original task fields unchanged.

## Step 3.6: Detect Fix / Follow-up Linkage

Before presenting the task list, scan each proposed task's **summary and description** for phrasing that signals it is a follow-up, rework, or fix of an earlier task:

- `fixes TASK-N`
- `follow-up from TASK-N` / `follow up from TASK-N`
- `retro follow-up from TASK-N`

Case-insensitive. When a match is found:

1. **Parse `N`** out of the phrase.
2. **Verify `N` exists** — check the backlog array from Step 2 first; if not present, run `tusk task-get N` to confirm it exists as a Done task. If `N` doesn't resolve to a real task, drop the linkage silently and continue (the phrasing was likely informal).
3. **Reject self-reference** — if `N` equals the proposed task's own future id (can't be known yet; use summary cross-check: if the input explicitly ties `N` to the same task being created, skip).
4. **Record `fixes_task_id = N`** on that task's metadata for use by Step 5.

**Ambiguity prompt** — if a single task's text mentions **two or more different** `TASK-N` identifiers via the above phrasing, do not auto-pick. Surface the ambiguity before Step 4:

> **Follow-up linkage ambiguous for task #<i>**: the description mentions both `TASK-<a>` and `TASK-<b>` as sources. Which one should this task link to? (Answer with the id, or say "none" to skip.)

Apply the user's answer to that task's `fixes_task_id` and continue.

**Borderline phrasing** — mere mentions like "see TASK-N" or "related to TASK-N" (without `fixes`, `follow-up from`, or `retro follow-up from`) do **not** qualify. Leave `fixes_task_id` unset in those cases.

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

### Deferred mode notice

If **deferred mode = on**, add a notice directly below the task list (before asking for confirmation):

> **Note: deferred mode is on — all tasks will be inserted with `--deferred` (60-day expiry, `[Deferred]` prefix).**

This lets the user opt out (e.g., by editing or cancelling) before insertion.

### For both paths

Wait for explicit user approval before proceeding. Do NOT insert anything until the user confirms.

## Step 5: Deduplicate, Insert, and Generate Criteria

For each approved task, generate **3–7 acceptance criteria** — concrete, testable conditions that define "done." Derive them from the description: each distinct requirement or expected behavior maps to a criterion. For **bug** tasks, include a criterion that the failure case is resolved. For **feature** tasks, include the happy path and at least one edge case. For any task that creates a new database table (or is in a schema-related domain), always include the criterion: "DOMAIN.md updated with schema entry for `<table_name>`".

### Dangerous Criterion Guard

Before inserting, apply these rules to every generated criterion:

**Prohibited patterns** — Never generate a criterion whose text contains any of the following. These commands run against the live environment and can destroy data or corrupt history:
- `tusk init --force` — wipes the live task database
- `git reset --hard` — discards uncommitted work
- `git push --force` / `git push -f` — overwrites remote history
- `rm -rf` — recursive deletion
- `DROP TABLE` / `DROP DATABASE` — destructive SQL

**Init verification redirect** — If the task involves verifying `tusk init` behavior (e.g., "init creates the schema correctly", "init recreates the DB under --force"), do **not** generate a criterion that runs `tusk init` against the live database. Instead, use the integration test suite, which spins up a temporary DB automatically:

> `python3 -m pytest tests/integration/ -k test_init -q` passes

**Warning on detection** — If any generated criterion matches a prohibited pattern, display a warning and stop before inserting:

> ⚠️ **Dangerous criterion detected**: The proposed criterion `"<criterion text>"` contains a destructive command (`<pattern>`). This would run against the live database and could cause data loss. Replace it with a safer alternative (e.g., an integration test assertion) before inserting.

Revise the criterion and present it to the user for approval before proceeding to insertion.

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

If the task was linked to a source task in Step 3.6, append `--fixes-task-id <N>` so the follow-up relationship is persisted to `tasks.fixes_task_id`:

```bash
tusk task-insert "<summary>" "<description>" \
  --priority "<priority>" \
  --domain "<domain>" \
  --task-type "<task_type>" \
  --complexity "<complexity>" \
  --criteria "<criterion 1>" \
  --fixes-task-id <N>
```

When **deferred mode = on**, append `--deferred` to every `tusk task-insert` call. This flag applies uniformly to all tasks in the batch — it cannot be set per-task mid-flow:

```bash
tusk task-insert "<summary>" "<description>" \
  --priority "<priority>" \
  --domain "<domain>" \
  --task-type "<task_type>" \
  --assignee "<assignee>" \
  --complexity "<complexity>" \
  --criteria "<criterion 1>" \
  --criteria "<criterion 2>" \
  --criteria "<criterion 3>" \
  --deferred
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

When **deferred mode = on**, label the created line as `**Created (deferred)**` instead of `**Created**`:

```markdown
**Created (deferred)**: 3 tasks (#14, #15, #16)
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
