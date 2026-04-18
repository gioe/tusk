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
- **D**: Lint Rules — concrete, grep-detectable anti-patterns (max 3). Only if an actual mistake occurred that a grep rule could prevent. Applied inline when possible (step 5d); task creation is the fallback.
- **E**: Debugging Velocity — only if the session involved fixing a bug or diagnosing unexpected behavior. Reflect on: (1) what information was missing that delayed diagnosis; what tool, log, or trace would have surfaced the root cause immediately; whether a test would have caught this before it became a bug. (2) Did fixing this bug change the conditions under which adjacent issues matter? (e.g., removing noise that was masking a separate signal, raising the quality bar in a way that exposes nearby gaps.) If so, those adjacent issues are in scope for this category even if they predate the session — "predated the session" is not sufficient grounds for dismissal when the fix elevated their relevance. If no bug was present, this category is empty. Findings must be concrete (tasks or skill/CLAUDE.md patches) — not generic advice like "add more logging."

If a category has no findings, note that explicitly — an empty category is a positive signal.

### 3a: Classify Each Finding

For each finding, determine whether it is a **tusk-issue** or a **project-issue**:

- **tusk-issue** — a bug, limitation, or improvement in tusk itself: the CLI, a skill, DB schema, or installed tooling (e.g., a skill instruction is confusing, a `tusk` command misbehaves, a missing feature in the tool)
- **project-issue** — specific to the current project: its code, architecture, conventions, or processes

Label each finding with its classification. This drives the routing in Step 5b.

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

(Repeat for each category. Use the resolved category names — from FOCUS.md if present, or defaults A/B/C/D/E. Omit empty categories.)

### Duplicates Already Tracked (omit if none)
| Finding | Matched Task | Similarity |
|---------|-------------|------------|

### Subsumed into Existing Tasks (omit if none)
| Finding | Merge Into | Reason | Proposed Amendment |
|---------|-----------|--------|-------------------|

### Proposed Actions (new work only)
| # | Summary | Priority | Domain | Type | Category | Classification |
|---|---------|----------|--------|------|----------|----------------|
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

### 5b: Insert New Tasks / File Issues

Route each approved finding based on its classification from Step 3a:

**tusk-issues** — file a GitHub issue via:
```bash
tusk report-issue --title "<finding title>" --context "<finding description>"
```
Do **not** call `tusk task-insert` for tusk-issues. Track the count of issues filed for Step 6.

**Include a `## Failing Test` section** in `--context` whenever a concrete test can be derived from the finding. This matters because `/address-issue` Factor 0 treats a missing failing test as the highest-priority signal to Defer — issues filed without one will be deprioritized automatically. Format:

```
<finding description>

## Failing Test

<shell command that currently fails or demonstrates the bug>
```

If no concrete test exists (e.g. a pure UX or documentation finding), omit the section rather than fabricating one.

**If `tusk report-issue` exits non-zero** (e.g., `$TUSK_GITHUB_REPO` is unset or `gh` CLI is unavailable), fall back to inserting a tusk task instead:
```bash
tusk task-insert "<finding title>" "<finding description> [Note: GitHub issue could not be filed — report-issue failed]" \
  --domain skills --task-type chore --priority Low --complexity XS \
  --criteria "File a GitHub issue for this finding once $TUSK_GITHUB_REPO is configured"
```
Note in Step 6 that the issue was tracked as a local task rather than filed on GitHub.

**project-issues** — **Category A and Category E findings:** Before inserting, follow step 5e to check for an inline skill patch. Only call `tusk task-insert` for a Category A or E finding here if step 5e was skipped, if no target file was identified, or if the user chose to defer (include the proposed diff in the description).

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

### 5d: Apply Lint Rules Inline (only if lint rule findings exist)

Apply this step if there are lint rule findings — Category D when using defaults, or a "Lint Rules" section when using a custom FOCUS.md.

The bar is high — only proceed if you observed an **actual mistake** that a grep rule would have caught. Do not apply lint rules for general advice.

For each lint rule finding, attempt **inline application** first:

1. **Present the proposed rule** — show the exact command and ask for approval:

   > Found lint rule candidate: [finding description]
   > Command: `tusk lint-rule add '<pattern>' '<file_glob>' '<message>'`
   > Apply this rule now? (Reversible with `tusk lint-rule remove <id>`.)

2. **If the user approves** — run the command immediately:
   ```bash
   tusk lint-rule add '<pattern>' '<file_glob>' '<message>'
   ```
   - **Success**: note the rule ID returned. **Do not create a task** for this finding.
   - **Error or unavailable**: fall back to task creation (step 3).

3. **If the user declines**, or **if inline application fails**, create a task as a fallback:
   ```bash
   tusk task-insert "Add lint rule: <short description>" \
     "Run: tusk lint-rule add '<pattern>' '<file_glob>' '<message>'" \
     --priority "Low" --task-type "<task_type>" --complexity "XS" \
     --criteria "tusk lint-rule add has been run with the specified pattern, glob, and message"
   ```

For `<task_type>`: use the project's config `task_types` array (already fetched via `tusk setup` in Step 0). Pick the entry that best fits a maintenance/tooling task (e.g., `maintenance`, `chore`, `tech-debt`, `infra` — whatever is closest in your project's list). If no entry is a clear fit, omit `--task-type` entirely.

Fill in `<pattern>` (grep regex), `<file_glob>` (e.g., `*.md` or `bin/tusk-*.py`), and `<message>` (human-readable warning) with the specific values from your finding.

### 5e: Skill-Patch for Category A and Category E Findings (only if Category A or Category E findings exist)

Before creating tasks for Category A (process improvement) or Category E (debugging velocity) findings, check if any can be applied as inline patches to an existing skill or CLAUDE.md. Run this step **before** 5b for Category A and Category E findings.

For each approved Category A finding:

1. **Classify the finding as rule-like or narrative:**
   - **Rule-like**: a single heuristic, invariant, or convention — e.g., "always quote file paths in zsh". These belong in the conventions DB.
   - **Narrative/reference**: multi-step procedures, workflow descriptions, or anything requiring more than one sentence. These belong as a patch to a skill file or CLAUDE.md.

2. **If the finding is rule-like** — propose adding a convention via `tusk conventions add`:
   a. Draft the exact convention text (one concise sentence) and a comma-separated list of relevant topic tags.
   b. Present the proposal with three options:

      > **Convention Proposal** — [finding title]
      >
      > ```
      > tusk conventions add "[concise rule text]" --topics "[tag1,tag2]"
      > ```
      >
      > **approve** — run the command now (no task created for this finding)
      > **defer** — create a task with this command included in the description (handled in 5b)
      > **skip** — create a generic task via 5b as usual

   c. **If approved**: run the command now using Bash. Do **not** create a task for this finding.
   d. **If deferred**: proceed to 5b for this finding, including the proposed command verbatim in the task description.
   e. **If skipped**: proceed to 5b normally.

3. **If the finding is narrative/reference** — identify a target file:
   - A skill name matching a directory in `.claude/skills/` (list them with `ls .claude/skills/`)
   - The string `CLAUDE.md`

   **If a target file is identified**:
   a. Read the file (`Read .claude/skills/<name>/SKILL.md` or `Read CLAUDE.md`)
   b. Produce a **concrete proposed edit** — the exact text to add, change, or remove. Show the specific diff, not a vague description.
   c. Present the patch with three options:

      > **Skill Patch Proposal** — [finding title]
      > File: `.claude/skills/<name>/SKILL.md`
      >
      > ```diff
      > - [existing text to replace]
      > + [replacement text]
      > ```
      >
      > **approve** — apply the edit now (no task created for this finding)
      > **defer** — create a task with this diff included in the description (handled in 5b)
      > **skip** — create a generic task via 5b as usual

   d. **If approved**: apply the edit in-session using the Edit tool. Do **not** create a task for this finding.
   e. **If deferred**: proceed to 5b for this finding, including the proposed diff verbatim in the task description.
   f. **If skipped, or if no target file was identified**: proceed to 5b normally.

## Step 6: Report Results

```markdown
## Retrospective Complete

**Session**: <what was accomplished>
**Findings**: N findings by category (use resolved category names)
**Created**: N tasks (#id, #id)
**GitHub issues filed**: N (tusk-issues routed via tusk report-issue — omit line if zero)
**Lint rules**: K applied inline, M deferred as tasks
**Subsumed**: S findings into existing tasks (#id)
**Dependencies added**: D (if any were created)
**Skipped**: M duplicates
```

Include **Dependencies added** only when Step 5c was executed. Omit if all tasks were duplicates/subsumed.

Then show the backlog:

```bash
tusk -header -column "SELECT id, summary, priority, domain, task_type, status FROM tasks WHERE status = 'To Do' ORDER BY priority_score DESC, id"
```

Finally, close out the retro skill-run so its cost is captured (uses the `run_id` captured in Step 0 of SKILL.md):

```bash
tusk skill-run finish <run_id>
```
