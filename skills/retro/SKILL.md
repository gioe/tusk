---
name: retro
description: Review the current session, surface process improvements and tangential issues, and create follow-up tasks
allowed-tools: Bash, Read
---

# Retrospective Skill

Reviews the current conversation history to capture process learnings, instruction improvements, and tangential issues discovered during the session. Creates structured follow-up tasks so nothing falls through the cracks.

## Step 0: Determine Retro Mode

Check the complexity of the task that was just completed:

```bash
tusk "SELECT complexity FROM tasks WHERE status = 'Done' ORDER BY updated_at DESC LIMIT 1"
```

- **XS or S** → follow the **Lightweight Retro** path below
- **M, L, XL, or NULL** → skip to **Step 1** (full retro)

---

## Lightweight Retro (XS/S tasks)

A streamlined retro for small tasks. Skips config reads, subsumption analysis, and dependency proposals — just scans for issues and creates tasks if needed.

### LR-1: Review & Categorize

Analyze the full conversation context. Look for:

- **Category A**: Process improvements — friction in skills, CLAUDE.md, tooling
- **Category B**: Tangential issues — bugs, tech debt, architectural concerns discovered out of scope
- **Category C**: Follow-up work — incomplete items, deferred decisions, edge cases

If **all categories are empty**, report "Clean session — no findings" and stop. Do not fetch config or backlog.

### LR-2: Create Tasks (only if findings exist)

1. Fetch config and open backlog:
   ```bash
   tusk config
   tusk -header -column "SELECT id, summary, domain, priority FROM tasks WHERE status <> 'Done' ORDER BY id"
   ```

2. Compare each finding against the backlog for semantic overlap. Drop any that are already covered by an existing task.

3. Run heuristic dupe check on surviving findings:
   ```bash
   tusk dupes check "<proposed summary>"
   ```

4. Present the retrospective report and proposed tasks table (same format as Step 4). Wait for explicit user approval before inserting.

5. Insert approved tasks (same as Step 5b). Skip subsumption and dependency proposals.

### LR-3: Report

```markdown
## Retrospective Complete (Lightweight)

**Session**: <what was accomplished>
**Findings**: X total (A process / B tangential / C follow-up)
**Created**: N tasks (#id, #id)
**Skipped**: M duplicates
```

Then show the current backlog:

```bash
tusk -header -column "SELECT id, summary, priority, domain, task_type, status FROM tasks WHERE status = 'To Do' ORDER BY priority_score DESC, id"
```

**End of lightweight retro.** Do not continue to Step 1.

---

## Full Retro (M / L / XL tasks)

## Step 1: Review Session History

Analyze the full conversation context. Look for:

- **Friction points** — Confusing instructions, missing context, repeated mistakes, unclear CLAUDE.md guidance
- **Workarounds** — Manual steps that could be automated, patterns that should be codified into skills or scripts
- **Tangential issues** — Test failures, architectural concerns, tech debt, or bugs discovered but not addressed because they were out of scope
- **Incomplete work** — Deferred decisions, TODO comments added, partial implementations, features that need follow-up
- **Failed approaches** — Strategies that didn't work and why, so they can be documented to prevent future reattempts

Be thorough — review the entire session, not just the most recent messages.

## Step 2: Read Project Config

Fetch valid values so any proposed tasks conform to the project's configured constraints:

```bash
tusk config
```

This returns the full config as JSON (domains, task_types, agents, priorities, complexity, etc.). Store the parsed values for use when assigning metadata. If a field is an empty list (e.g., `"domains": []`), that field has no validation — use your best judgment or leave it NULL.

## Step 2b: Fetch Existing Backlog

Fetch all open tasks so you can cross-reference proposed findings against them for semantic overlap:

```bash
tusk -header -column "SELECT id, summary, domain, priority FROM tasks WHERE status <> 'Done' ORDER BY id"
```

Hold these in context for Step 3. When categorizing findings, compare each proposed task against this list. If an existing task already covers the same intent — even with different wording — note it as already tracked rather than proposing a new task. The heuristic dupe checker (Step 3b) catches textual near-matches, but you can catch **semantic** duplicates it would miss.

## Step 3: Categorize Findings

Organize findings into three categories:

### Category A: Process Improvements

Changes to skills, CLAUDE.md, project documentation, or tooling that would have made the session smoother. Examples:

- A CLAUDE.md instruction that was misleading or missing
- A skill that could be added or improved
- A convention that should be documented
- A config change that would prevent a class of errors

### Category B: Tangential Issues

Problems discovered during the session that were out of scope but need tracking. Examples:

- A test that was failing for unrelated reasons
- Tech debt noticed while reading code
- An architectural concern that surfaced during implementation
- A bug in a different part of the system

### Category C: Follow-up Work

Incomplete items, deferred decisions, or next steps from the session. Examples:

- A feature that was partially implemented
- A decision that was punted to a future session
- An enhancement that was discussed but not started
- Edge cases identified but not handled

If a category has no findings, note that explicitly — an empty category is a positive signal.

## Step 3b: Pre-filter Duplicates (Heuristic)

Semantic duplicates should already have been filtered out during Step 3 (by comparing against the backlog from Step 2b). As a deterministic safety net for textual near-matches, run a heuristic duplicate check on every proposed task summary:

```bash
tusk dupes check "<proposed summary>"
# Include --domain if set:
tusk dupes check "<proposed summary>" --domain <domain>
```

- **Exit code 0 (no duplicate):** Keep the finding in the proposed tasks table.
- **Exit code 1 (duplicate found):** Remove the finding from the proposed tasks table. Record the match (existing task ID, similarity score) for the report. Do NOT create a task for it.
- **Exit code 2 (error):** Keep the finding in the proposed table and let Step 5 handle it.

This ensures the proposed tasks table shown to the user contains only genuinely new work.

## Step 3c: Subsumption Check

Some findings may not be duplicates but are closely related to an existing open task — close enough that they should be folded into that task's scope rather than filed separately. For each proposed finding that passed the duplicate check, evaluate whether it should be **subsumed** into an existing task instead of creating a new ticket.

**Subsumption criteria** (if two or more apply, recommend subsumption):

- **Same file or module** — the finding and the existing task modify the same file(s) or module
- **Same PR** — a single PR would naturally address both items
- **Small relative scope** — the new finding is a minor addition compared to the existing task's scope
- **Same domain and goal** — they share the same domain and conceptual objective

For each subsumed finding, record:
- The existing task ID it should be merged into
- A proposed amendment to append to that task's description (a concise paragraph describing the additional work)

Subsumed findings are removed from the proposed tasks table and shown in their own section of the report. They will be handled in Step 5 by updating the existing task's description rather than inserting a new row.

## Step 4: Present Retrospective Report

Show all findings in a structured report. For each finding that warrants a task, include a proposed task row. Findings that matched an existing task in Step 3b are reported separately — they do not appear in the proposed tasks table. Findings recommended for subsumption (Step 3c) are shown in their own section with the proposed description amendment.

```markdown
## Session Retrospective

### Summary
Brief (2-3 sentence) overview of what the session accomplished and overall observations.

### Category A: Process Improvements (N findings)

1. **<finding title>**
   <description of the friction point or improvement opportunity>
   → Proposed task: <summary> | Priority: <priority> | Type: <task_type> | Domain: <domain>

2. ...

### Category B: Tangential Issues (N findings)

1. **<finding title>**
   <description of the issue discovered>
   → Proposed task: <summary> | Priority: <priority> | Type: <task_type> | Domain: <domain>

2. ...

### Category C: Follow-up Work (N findings)

1. **<finding title>**
   <description of the incomplete or deferred work>
   → Proposed task: <summary> | Priority: <priority> | Type: <task_type> | Domain: <domain>

2. ...

### Duplicates Already Tracked

If any findings matched existing tasks (from Step 3b), list them here:

| Finding | Matched Task | Similarity |
|---------|-------------|------------|
| <proposed summary> | #<id> — <existing summary> | 0.XX |

If no duplicates were found, omit this section.

### Subsumed into Existing Tasks

If any findings were recommended for subsumption (from Step 3c), list them here:

| Finding | Merge Into | Reason | Proposed Amendment |
|---------|-----------|--------|-------------------|
| <proposed summary> | #<id> — <existing summary> | <which criteria matched> | <text to append to existing task description> |

If no findings were subsumed, omit this section.

### Proposed Tasks

Only genuinely new tasks appear here (those that passed the duplicate check):

| # | Summary | Priority | Domain | Type | Category |
|---|---------|----------|--------|------|----------|
| 1 | ... | ... | ... | ... | A/B/C |
| 2 | ... | ... | ... | ... | A/B/C |
```

Then ask:

> Does this look right? You can:
> - **Confirm** to create all new tasks and apply all subsumptions
> - **Remove** specific numbers (e.g., "remove 3")
> - **Edit** a task (e.g., "change 2 priority to High")
> - **Reject subsumption** (e.g., "don't merge finding X into #42" — it will become a new task instead)
> - **Add** a finding I missed
> - **Skip** to end the retro without creating tasks

Wait for explicit user approval before proceeding. Do NOT insert anything until the user confirms.

## Step 5: Apply Approved Changes

### 5a: Apply Subsumptions

For each approved subsumption, append the proposed amendment to the existing task's description:

```bash
EXISTING_DESC=$(tusk "SELECT description FROM tasks WHERE id = <existing_task_id>")
AMENDED_DESC="${EXISTING_DESC}

---
Subsumed from retro finding: <finding summary>
<proposed amendment text>"
tusk "UPDATE tasks SET description = $(tusk sql-quote "$AMENDED_DESC"), updated_at = datetime('now') WHERE id = <existing_task_id>"
```

Report each update:

> Merged finding into task #<id>: appended scope amendment to description.

### 5b: Insert New Tasks

Duplicates were already filtered out via LLM semantic review (Step 3) and heuristic pre-filter (Step 3b). Insert each approved task directly.

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
    '<complexity_or_NULL>',
    datetime('now'),
    datetime('now')
  )"
```

Use `$(tusk sql-quote "...")` for any field that may contain freeform text (summary, description). Static values from config (priority, domain, task_type, assignee) don't need quoting since they come from validated config values.

For NULL fields, use the literal `NULL` (unquoted) — don't pass it through `sql-quote`:

```bash
tusk "INSERT INTO tasks (summary, description, status, priority, domain, task_type, assignee, complexity, created_at, updated_at)
  VALUES ($(tusk sql-quote "Improve error handling docs"), $(tusk sql-quote "Details here"), 'To Do', 'Medium', NULL, 'feature', NULL, NULL, datetime('now'), datetime('now'))"
```

### 5c: Propose Dependencies

After inserting tasks, check whether any dependencies exist — both among the newly created retro tasks **and** against existing open backlog tasks (fetched in Step 2b). Retro tasks tend to be independent, so this step is lightweight: only propose a dependency when there is a clear, concrete reason one task must complete before the other can begin.

#### When to run this step

- If **two or more tasks** were created: check for inter-task ordering among the new tasks AND for dependencies on existing backlog tasks.
- If **one task** was created: check for dependencies on existing backlog tasks only.
- Skip this step entirely if zero tasks were created (all were duplicates or subsumed).

#### How to identify dependencies

Look for these patterns:

- **Process change before feature** — a CLAUDE.md or skill update (Category A) that must land before a feature task (Category C) can follow the updated process
- **Bug fix before follow-up** — a tangential bug fix (Category B) that blocks related follow-up work (Category C)
- **Schema/infra before code** — a migration or config change that must exist before dependent tasks can proceed
- **New task depends on existing backlog task** — a retro finding that extends or builds on work already tracked in the backlog

If no natural ordering exists, state that explicitly and skip to Step 6.

#### Present proposed dependencies

Show a numbered table of proposed dependencies for user approval:

```markdown
## Proposed Dependencies

| # | Task | Depends On | Type | Reason |
|---|------|------------|------|--------|
| 1 | #16 Add signup validation (new) | #14 Fix auth middleware (new) | blocks | Validation requires working auth |
| 2 | #15 Update API docs (new) | #8 Refactor endpoints (existing) | contingent | Docs are moot if refactor is cancelled |
```

Then ask:

> Does this look right? You can:
> - **Confirm** to add all dependencies
> - **Remove** specific numbers (e.g., "remove 2")
> - **Change type** (e.g., "change 1 to contingent")
> - **Skip** to add no dependencies

Wait for explicit user approval before inserting.

#### Insert approved dependencies

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
## Retrospective Complete

**Session**: <brief description of what was accomplished>
**Findings**: A process improvements, B tangential issues, C follow-up items
**Created**: N tasks (#14, #15, #16)
**Subsumed**: S findings merged into existing tasks (#8, #11)
**Dependencies added**: D (#16 → #14 (blocks), #15 → #8 (contingent))
**Skipped**: M duplicates

| ID | Summary | Priority | Domain | Category |
|----|---------|----------|--------|----------|
| 14 | ... | ... | ... | A |
| 15 | ... | ... | ... | B |
| 16 | ... | ... | ... | C |

If any findings were subsumed:

| Finding | Merged Into | Amendment |
|---------|-----------|-----------|
| <finding summary> | #<id> | <brief description of what was added> |
```

Include the **Dependencies added** line only when Step 5c was executed (i.e., at least one task was created). If Step 5c was skipped (all tasks were duplicates/subsumed) or the user chose to skip all dependencies, omit the line. If dependencies were proposed but the user removed some, only list the ones actually inserted.

Then show the current backlog state:

```bash
tusk -header -column "SELECT id, summary, priority, domain, task_type, status FROM tasks WHERE status = 'To Do' ORDER BY priority_score DESC, id"
```

## Important Guidelines

- **All DB access goes through `tusk`** — never use raw `sqlite3`
- **Always confirm before inserting** — never insert tasks without explicit user approval
- **Always run dupe checks** — check every task against existing open tasks before inserting
- **Use `tusk sql-quote`** — always wrap user-provided text with `$(tusk sql-quote "...")` in SQL statements
- **Leave `priority_score` at 0** — let `/groom-backlog` compute scores later
- **Use configured values only** — read domains, task_types, agents, and priorities from `tusk config`, never hardcode
- **Be honest about empty categories** — if the session went smoothly, say so; don't manufacture findings
- **Focus on actionable items** — every finding should either become a task or explicitly explain why no task is needed
- **Review the entire session** — don't just look at the last few messages; friction early in the session is just as valuable
