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

## Step 2b: Fetch Retro Signals

Fetch pre-aggregated retro signals for the just-closed task. `RETRO_TASK_ID` was captured in Step 0 of SKILL.md:

```bash
tusk retro-signals $RETRO_TASK_ID
```

Parse the JSON. The fields consumed by the steps below are:

- **`review_themes`** — `(category, severity)` pairs with ≥ 2 occurrences across this task's review passes, plus a short sample comment. Each theme is a candidate Category A (conventions) or Category D (lint rules) finding. Seed Step 3 directly from this list — **do not** re-query `review_comments` with SQL.
- **`deferred_review_comments`** — individual review comments with `resolution='deferred'`, each with its `deferred_task_id` (may be null). These are open follow-up threads and must be surfaced in the Step 4 report so reviewers can see what was punted forward.
- **`reopen_count`** — integer count of `to_status='To Do'` transitions on this task. When > 0, render a `**Reopened N times**` line in Step 4's "Rework Context" section so the reviewer pauses on whether the close actually stuck.
- **`rework_chain`** — `{fixes, fixed_by}`. `fixes` is the upstream task this one was filed to address (via `fixes_task_id`); `fixed_by` is the downstream follow-ups that were filed to fix *this* one. When either list is non-empty, render the entries in Step 4's "Rework Context" section and append the explicit "**Was the root cause addressed?**" prompt — recurring fix chains are the strongest signal that an earlier pass treated symptoms rather than the underlying issue.

Consume `reopen_count` and `rework_chain` from this same `tusk retro-signals` JSON — **do not** issue separate SQL queries against `task_status_transitions` or `tasks.fixes_task_id`. The aggregation already covers both directions.

When `reopen_count == 0` AND both `rework_chain.fixes` and `rework_chain.fixed_by` are empty, omit the "Rework Context" section from Step 4 silently (no heading, no placeholder).

- **`skipped_criteria`** — acceptance criteria with a non-empty `skip_note` (covers both `is_deferred=1` deferrals and `--skip-verify` closures that recorded a rationale). Each entry is a gap the author acknowledged at close time and must be surfaced in Step 4's "Known gaps at close" section so the reviewer can decide whether the skip is acceptable or needs a follow-up task.
- **`unconsumed_next_steps`** — every non-empty `task_progress.next_steps` handoff note for this task, oldest first. Many of these describe work that was later completed in the same session; some describe work that was quietly dropped. Step 4 runs a heuristic match against the final committed work and surfaces the residue under "Known gaps at close" — ambiguous matches must trigger a user confirmation prompt before being called out.

Consume `skipped_criteria` and `unconsumed_next_steps` from this same `tusk retro-signals` JSON — **do not** issue separate SQL queries against `acceptance_criteria` or `task_progress`. The aggregation already filters empty `skip_note`/`next_steps` rows for you.

- **`tool_call_outliers`** — tools whose `SUM(call_count)` across every session belonging to `RETRO_TASK_ID` met or exceeded a per-complexity threshold. `retro-signals` does the `tool_call_stats` aggregation, the session-grain filter (to avoid double-counting across the `(session, skill_run, criterion)` denormalization), and the threshold cut in SQL; each row is `{tool_name, call_count, total_cost, threshold, complexity}`. Drives Step 4's "Session Shape" section as a **soft warning only** — /retro does not auto-create a task from this signal, it's context for the reviewer to decide whether the shape of the session was healthy.

  The per-complexity thresholds (tunable via `CALL_COUNT_THRESHOLDS` in `bin/tusk-retro-signals.py`) are:

  | Complexity | `call_count` threshold |
  |------------|------------------------|
  | XS         | 20                     |
  | S          | 40                     |
  | M          | 80                     |
  | L          | 150                    |
  | XL         | 300                    |
  | (unset)    | 80                     |

Consume `tool_call_outliers` from this same `tusk retro-signals` JSON — **do not** issue a separate SQL query against `tool_call_stats`. The aggregation already applies the session-grain filter and the per-complexity threshold for you.

This signal drives the full retro only — the lightweight retro path (XS/S in `SKILL.md`) is intentionally unchanged to keep it lean.

- **`tool_errors`** — tool failures observed during this task's sessions, aggregated per `tool_name`. Data source is the Claude Code transcript (`~/.claude/projects/*.jsonl`), not a DB table — every failing tool_use already lands in the transcript with `is_error: true`, so no PostToolUse hook or sidecar log file is involved. See `docs/retro-error-detection.md` for the evaluation that picked this path over a hook. Each row is `{tool_name, error_count, sample}` where `sample` is the first observed error for that tool (trimmed to ~160 chars; the `<tool_use_error>` wrapper is stripped when present). Rows are already sorted by `error_count` descending, then `tool_name`. Drives Step 4's "Errors encountered" section as a **soft warning only** — /retro does not auto-create a task from this signal, it's context for the reviewer to decide whether the failures were meaningful (a real bug) or benign (a typo that got corrected on the next call).

Consume `tool_errors` from this same `tusk retro-signals` JSON — **do not** open transcripts directly or issue separate `tool_call_stats` queries. The aggregation already resolves the tool_use_id → tool_name mapping, applies the session-window filter, and truncates the sample for you.

This signal drives the full retro only — the lightweight retro path (XS/S in `SKILL.md`) is intentionally unchanged to keep it lean.

If the task has no review activity at all, both `review_themes` and `deferred_review_comments` will be empty arrays. In that case, **omit** the "Review Theme Summary" section from Step 4 silently — do not add a "(none)" placeholder.

## Step 2c: Cross-retro Themes (from Step 0b output)

Step 0b in `SKILL.md` already ran `tusk retro-themes --window-days 30 --min-recurrence 3` and captured the pre-aggregated `{theme, count}` tuples as `$RECURRING_THEMES`. **Do not re-run the query here**, and **do not** issue separate SQL against `retro_findings` — all cross-retro aggregation belongs behind `tusk retro-themes`; `/retro` consumes only the tuple stream.

If `$RECURRING_THEMES` is empty, no recurring pattern has crossed the 3×/30-day bar yet — nothing to flag in Step 3.

If one or more themes are present, carry the list into Step 3: for every finding whose category matches a recurring theme, append an inline recurrence note (`— recurring theme: seen N times in last 30 days`) next to that finding in both the categorization table and the Step 4 report. This tells the reviewer "this isn't the first time we've surfaced something in this bucket" before they approve a new task for it, which raises the bar for duplicate work.

## Step 3: Categorize Findings

If `<base_directory>/FOCUS.md` was found in Step 1, use those categories.

Otherwise organize into the default four categories:

- **A**: Process improvements — skill/CLAUDE.md/tooling friction, confusing instructions, missing conventions
- **B**: Tangential issues — out-of-scope bugs, tech debt, architectural concerns
- **C**: Follow-up work — incomplete items, deferred decisions, edge cases
- **D**: Lint Rules — concrete, grep-detectable anti-patterns (max 3). Only if an actual mistake occurred that a grep rule could prevent. Applied inline when possible (step 5d); task creation is the fallback.
- **E**: Debugging Velocity — only if the session involved fixing a bug or diagnosing unexpected behavior. Reflect on: (1) what information was missing that delayed diagnosis; what tool, log, or trace would have surfaced the root cause immediately; whether a test would have caught this before it became a bug. (2) Did fixing this bug change the conditions under which adjacent issues matter? (e.g., removing noise that was masking a separate signal, raising the quality bar in a way that exposes nearby gaps.) If so, those adjacent issues are in scope for this category even if they predate the session — "predated the session" is not sufficient grounds for dismissal when the fix elevated their relevance. If no bug was present, this category is empty. Findings must be concrete (tasks or skill/CLAUDE.md patches) — not generic advice like "add more logging."

If a category has no findings, note that explicitly — an empty category is a positive signal.

### Seeding from `review_themes`

For each entry in `review_themes` (from Step 2b), add one candidate finding to either Category A or Category D based on the theme's `category`/`severity` signal and its sample comment:

- **Category A (conventions)** — the recurring comment describes a heuristic, preference, or convention the reviewer keeps repeating (e.g. "always pass `encoding='utf-8'` to `subprocess.run`", "prefer `pathlib.Path` over `os.path`"). Rule-like guidance that can be captured via `tusk conventions add` belongs here.
- **Category D (lint rules)** — the recurring comment points at a concrete grep-detectable anti-pattern (e.g. "don't call `sqlite3` directly", "bare `except:` in *.py files"). Only promote to D if an actual mistake occurred that a grep rule would have caught — general advice stays in A.

Use the `count` and `sample` fields to show the reviewer why this theme crossed the noise floor. Don't invent themes that aren't in `review_themes` — the aggregation already filtered to recurrence ≥ 2.

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

### Rework Context (omit if reopen_count == 0 AND rework_chain.fixes is empty AND rework_chain.fixed_by is empty)

- **Reopened N times** (omit line if reopen_count == 0)
- **Fixes**: TASK-X "<summary>" (status) (one bullet per `rework_chain.fixes` entry; omit line if empty)
- **Fixed by**: TASK-Y "<summary>" (status) (one bullet per `rework_chain.fixed_by` entry; omit line if empty)

> **Was the root cause addressed?** (include only when `rework_chain.fixes` or `rework_chain.fixed_by` is non-empty)

### Review Theme Summary (omit if both tables below are empty)

**Recurring themes** (from `review_themes` — omit table if empty)
| Category | Severity | Count | Sample |
|----------|----------|-------|--------|

**Deferred review comments** (from `deferred_review_comments` — omit table if empty)
| # | Category | Severity | File | Deferred Task | Comment |
|---|----------|----------|------|---------------|---------|

### Known gaps at close (omit if skipped_criteria is empty AND no next_steps survive the heuristic match)

**Skipped criteria** (from `skipped_criteria` — omit table if empty)
| # | Criterion | Kind | Skip note |
|---|-----------|------|-----------|

**Unfinished next_steps** (from `unconsumed_next_steps` after heuristic match — omit table if empty)
| When | Handoff note |
|------|--------------|

### Session Shape (omit if tool_call_outliers is empty)

> **Soft warning** — these counts are context, not an action item. /retro does not auto-create a task from this section.

| Tool | Calls | Threshold (complexity) | Cost |
|------|-------|------------------------|------|

### Errors encountered (omit if tool_errors is empty)

> **Soft warning** — these errors are context, not an action item. /retro does not auto-create a task from this section.

| Tool | Errors | Sample |
|------|-------:|--------|

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

**Review Theme Summary rendering rules:**
- If both `review_themes` and `deferred_review_comments` are empty, omit the entire "Review Theme Summary" section silently (no heading, no placeholder).
- If only one of the two tables has rows, include the section with just that table and omit the empty one.
- Each `deferred_review_comments` row shows `deferred_task_id` as `TASK-<id>` when non-null; render `—` when null (no follow-up task was linked). This keeps the "what happened to it" link visible even after the comment is closed out.
- Sample columns are already truncated to 80 chars by `retro-signals`; do not re-quote or pad them.

**Rework Context rendering rules:**
- If `reopen_count == 0` AND both `rework_chain.fixes` and `rework_chain.fixed_by` are empty, omit the entire "Rework Context" section silently (no heading, no placeholder).
- The "Reopened N times" line appears only when `reopen_count > 0`.
- The "Fixes" / "Fixed by" bullets appear only when their respective list is non-empty. Render each entry's `id` as `TASK-<id>` and include the `status`.
- When either `rework_chain.fixes` or `rework_chain.fixed_by` is non-empty, the "Was the root cause addressed?" prompt is mandatory — it's the entire reason for surfacing the chain.

**Known gaps at close rendering rules:**
- Before rendering the "Unfinished next_steps" table, classify each `unconsumed_next_steps` entry against the session's actual outcomes (completed criteria, commit messages, final merge state):
  - **Matched** — the handoff note is clearly reflected in work that shipped. Drop it silently.
  - **Unmatched** — the handoff note describes work with no corresponding output. Keep it in the table.
  - **Ambiguous** — partial or indirect match (e.g. the note says "wire X to Y" and commits touched X but not Y). Ask the user `Did you consume this next_steps note? → "<quoted text>"` and wait for a yes/no answer before rendering. Keep the row only if the user says no.
- Each `skipped_criteria` row renders the `Kind` column as `deferred` when `is_deferred == 1` and `skipped` otherwise. The `Skip note` column is printed verbatim — the aggregation already guarantees it's non-empty.
- The "When" column for an `unconsumed_next_steps` row is the entry's `created_at` timestamp; the "Handoff note" column is the `next_steps` text verbatim (do not truncate).
- If `skipped_criteria` is empty AND every `unconsumed_next_steps` entry was either matched or confirmed-consumed via the prompt, omit the entire "Known gaps at close" section silently (no heading, no placeholder).
- If only one of the two tables has rows, include the section with just that table and omit the empty one.

**Session Shape rendering rules:**
- If `tool_call_outliers` is empty, omit the entire "Session Shape" section silently (no heading, no placeholder, no "clean session" note). An empty array means no tool crossed the threshold — or that `tool_call_stats` had no rows for any of the task's sessions at all, which is the same outcome from /retro's perspective.
- Each row renders `tool_name` verbatim, `call_count` as an integer, the `Threshold (complexity)` column as `<threshold> (<complexity>)` using the entry's own fields (e.g. `80 (M)`; render complexity as `unset` when null), and `total_cost` rounded to cents (`$0.42`). Rows are already sorted descending by `call_count` by `retro-signals`; do not re-sort.
- The "Soft warning" callout is mandatory whenever the section is rendered — it's what flags this as context rather than a proposed action.
- Never promote a Session Shape row into a Proposed Action, a subsumption, or a lint rule. /retro treats this section as read-only diagnostic output: the reviewer decides whether the shape warrants follow-up, and if it does, they create the task manually via `/create-task`.

**Errors encountered rendering rules:**
- If `tool_errors` is empty, omit the entire "Errors encountered" section silently (no heading, no placeholder, no "clean session" note). An empty array means no failing `tool_result` landed inside any of the task's session windows — either no errors occurred, no transcripts were found for the project, or `task_sessions` had no rows with a `started_at` to scope against. /retro's behavior is identical in every case.
- Each row renders `tool_name` verbatim (including `(unknown)` when the originating `tool_use` block is missing from the transcript — typically because the session was split across a compaction or crash), `error_count` as an integer, and `sample` printed verbatim. The aggregation already truncates `sample` to the configured limit; do not re-quote or re-truncate. Rows are already sorted descending by `error_count`, tie-broken by `tool_name`, by `retro-signals`; do not re-sort.
- The "Soft warning" callout is mandatory whenever the section is rendered — same rule as Session Shape: it's what flags this as context rather than a proposed action.
- Never promote an Errors-encountered row into a Proposed Action, a subsumption, or a lint rule. One-off tool errors are frequently benign (a typo in a Bash command, a Read against a file that was already stale) — the reviewer is the one who decides whether a pattern warrants follow-up, and if so, they create the task manually via `/create-task`.

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

The /tusk skill already printed the task summary block (`tusk task-summary <id> --format markdown`) immediately before invoking /retro, so the user has already seen the canonical identity/cost/duration/diff/criteria rollup for the just-closed task. Do **not** re-emit that block here — start directly with the retrospective findings so the two sections read as one continuous report.

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

### 6a: Record approved findings for cross-retro theme detection

Before closing the skill run, write one `retro_findings` row per **approved** finding — every new task created in 5b, every GitHub issue filed, every lint rule applied in 5d, every convention or skill patch applied in 5e. Subsumptions (5a) are recorded as well, with `action_taken: subsumed:TASK-<id>` so the merge-into target is captured. Duplicates and user-rejected findings are **not** recorded — only approved, actioned findings feed the cross-retro signal.

For each approved finding, run:

```bash
tusk retro-finding add \
  --skill-run-id <run_id> \
  --category '<category>' \
  --summary '<one-line summary>' \
  [--task-id <RETRO_TASK_ID>] \
  [--action-taken '<action_taken>']
```

`<action_taken>` vocabulary (pick whichever fits; omit `--action-taken` if none do):
- `task:TASK-<id>` — a new task was created via `tusk task-insert`
- `issue:<url>` — a GitHub issue was filed via `tusk report-issue`
- `lint:<id>` — a lint rule was added via `tusk lint-rule add`
- `convention:<id>` — a convention was added via `tusk conventions add`
- `skill-patch:<file>` — an inline edit was applied to a skill or CLAUDE.md
- `subsumed:TASK-<id>` — folded into an existing task via 5a
- `documented` — recorded without a concrete action (e.g. noted for context)

**Omit** `--task-id` entirely when no `RETRO_TASK_ID` was captured in Step 0 of SKILL.md — the wrapper stores a real SQL NULL. Do not pass `--task-id NULL` or `--task-id ""`. Text fields are passed as normal argparse arguments; no `$(tusk sql-quote ...)` is required. The wrapper validates `skill_run_id` (and `task_id` if supplied) as real FKs before the INSERT, so a typo'd id fails fast with exit 1.

The `--category` value is the theme dimension `tusk retro-themes` groups on, so it must carry the resolved category name from Step 3 (default `A`/`B`/`C`/`D`/`E`, or the FOCUS.md label if customised). Do not write a human-readable category description here; downstream grouping is exact-match on this field.

Finally, close out the retro skill-run so its cost is captured (uses the `run_id` captured in Step 0 of SKILL.md):

```bash
tusk skill-run finish <run_id>
```
