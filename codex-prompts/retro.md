# Retro — Session Retrospective + Cross-Retro Themes (Codex)

Reviews the just-closed task to capture process learnings, instruction
improvements, and tangential issues. Creates structured follow-up tasks
so nothing falls through the cracks. Pulls per-task signals and
cross-retro themes from a single CLI orchestrator.

> **Conventions:** Run `tusk conventions search <topic>` for project
> rules. Do not restate convention text inline — it drifts from the DB.

> Use `create-task.md` for task creation — handles decomposition,
> deduplication, criteria, and deps. Use `tusk task-insert` only for
> bulk/automated inserts.

## Step 0: Setup

Capture the most recent Done task — the retro anchor — and start cost
tracking:

```bash
tusk "SELECT id, complexity FROM tasks WHERE status = 'Done' ORDER BY updated_at DESC LIMIT 1"
```

Store the returned `id` as `RETRO_TASK_ID` and `complexity` as
`RETRO_COMPLEXITY`. If the query returned no rows (no Done tasks exist
yet), set both to null and start the run with no `--task-id`:

```bash
tusk skill-run start retro
# or, when RETRO_TASK_ID is non-null:
tusk skill-run start retro --task-id $RETRO_TASK_ID
```

Capture `run_id` from the output — needed at every exit path below.

> **Early-exit cleanup:** If any step below causes the retro to stop
> before reaching the final report (LR-3 for lightweight, FR-6 for
> full), first call `tusk skill-run cancel <run_id>` to close the open
> row, then stop. Otherwise the row lingers as `(open)` in
> `tusk skill-run list` forever.

## Step 0a: Run the CLI Orchestrator

```bash
tusk retro $RETRO_TASK_ID --window-days 30 --min-recurrence 3
```

`tusk retro` bundles the per-task `retro-signals` rollup and the
cross-retro `retro-themes` aggregation into a single JSON document so
the prompt makes one subprocess call instead of two. Output keys:

- `signals` — task-specific signals: `complexity`, `reopen_count`,
  `rework_chain` (`fixes` / `fixed_by`), `review_themes`,
  `deferred_review_comments`, `skipped_criteria`, `tool_call_outliers`,
  `unconsumed_next_steps`.
- `themes` — array of `{theme, count}` tuples for findings recurring at
  least `--min-recurrence` times in the last `--window-days` days. Empty
  array when no themes meet the threshold.

If `RETRO_TASK_ID` is null, `tusk retro` will refuse — call
`tusk retro-themes --window-days 30 --min-recurrence 3` instead and treat
`signals` as null. Hold the parsed result in memory for the rest of the
flow.

Treat the `themes` array as the recurring-theme signal everywhere this
prompt mentions cross-retro themes. **Do not** issue separate SQL
queries against `retro_findings` — all aggregation belongs behind
`tusk retro`.

## Step 0b: Fetch Config and Backlog

```bash
tusk setup
```

Parse the JSON: use `config` for metadata assignment (priorities,
domains, agents, task_types) and `backlog` for duplicate comparison.

## Step 0c: Choose Retro Path

Based on `RETRO_COMPLEXITY` from Step 0:

- **XS or S** → follow the **Lightweight Retro** path below (LR-1
  through LR-3a).
- **M, L, XL, or NULL** → follow the **Full Retro** path below (FR-1
  through FR-6).

Both paths share Step 0–0c above.

---

## Lightweight Retro (XS/S tasks)

Streamlined retro for small tasks. Skips subsumption analysis and
dependency proposals.

### LR-1: Review & Categorize

Use the default categories:

- **Category A** — Process improvements: friction in prompts, agent
  doc, tooling.
- **Category B** — Tangential issues: bugs, tech debt, architectural
  concerns discovered out of scope.
- **Category C** — Follow-up work: incomplete items, deferred
  decisions, edge cases.
- **Category D** — Lint Rules: concrete, grep-detectable anti-patterns
  observed in this session (max 3). Only include if an actual mistake
  occurred that a grep rule could prevent.
- **Category E** — Debugging Velocity: only if the session involved
  fixing a bug or diagnosing unexpected behavior. Reflect on what
  information was missing that delayed diagnosis, what tool/log/trace
  would have surfaced the root cause immediately, and whether a test
  would have caught the bug before it became one. If the fix elevated
  the relevance of adjacent issues that predated the session, those
  count too. Findings must be concrete (tasks or skill/doc patches) —
  not generic advice like "add more logging."

Analyze the full conversation context using these categories.

If **all categories are empty**, run `tusk skill-run cancel <run_id>`,
report "Clean session — no findings" and stop.

When `themes` from Step 0a is non-empty, flag any finding whose
category matches a recurring theme — note "theme `<name>` recurring —
seen N times in last 30 days" next to that finding in the report.

### LR-1b: Classify Each Finding

For each finding, determine whether it is a **tusk-issue** or a
**project-issue**:

- **tusk-issue** — a bug, limitation, or improvement in tusk itself:
  the CLI, a prompt, DB schema, or installed tooling.
- **project-issue** — specific to the current project: its code,
  architecture, conventions, or processes.

Label each finding with its classification. This drives the routing in
LR-2.

### LR-2: Create Tasks / File Issues (only if findings exist)

1. Compare each finding against the backlog from Step 0b for semantic
   overlap. Drop any already covered.

2. Run heuristic dupe check on surviving findings:
   ```bash
   tusk dupes check "<proposed summary>"
   ```

3. Present findings and proposed actions in a table (include the
   classification from LR-1b). Wait for explicit user approval before
   acting.

4. For each approved finding, route based on its LR-1b classification:

   **tusk-issues** — file a GitHub issue via:
   ```bash
   tusk report-issue --title "<finding title>" --context "<finding description>"
   ```
   Do **not** call `tusk task-insert` for tusk-issues. Track the count of
   issues filed for LR-3.

   **Include a `## Failing Test` section** in `--context` whenever a
   concrete test can be derived from the finding (so
   `address-issue.md` Factor 0 doesn't auto-defer it):

   ```
   <finding description>

   ## Failing Test

   <shell command that currently fails or demonstrates the bug>
   ```

   If `tusk report-issue` exits non-zero (e.g., `$TUSK_GITHUB_REPO` is
   unset or `gh` is unavailable), fall back to inserting a tusk task:
   ```bash
   tusk task-insert "<finding title>" "<finding description> [Note: GitHub issue could not be filed — report-issue failed]" \
     --domain skills --task-type chore --priority Low --complexity XS \
     --criteria "File a GitHub issue for this finding once $TUSK_GITHUB_REPO is configured"
   ```
   Note in LR-3 that the issue was tracked as a local task rather than
   filed on GitHub.

   **project-issues** — For **Category A and Category E** approved
   findings, follow **LR-2a** below before inserting tasks. For all
   other project-issue findings, insert tasks now:
   ```bash
   tusk task-insert "<summary>" "<description>" \
     --priority "<priority>" --domain "<domain>" --task-type "<task_type>" \
     --assignee "<assignee>" --complexity "<complexity>" \
     --criteria "<criterion 1>" [--criteria "<criterion 2>" ...]
   ```
   Always include at least one `--criteria` flag — derive 1–3 concrete
   acceptance criteria from the task description. Omit `--domain` or
   `--assignee` entirely if the value is NULL/empty. Exit code 1 means
   duplicate — skip.

### LR-2a: Convention / Prompt-Patch for Category A and E Findings

Before creating tasks for Category A (process improvement) or Category
E (debugging velocity) findings, check if any can be applied as inline
fixes.

For each approved Category A finding:

1. **Classify the finding as rule-like or narrative:**
   - **Rule-like** — a single heuristic, invariant, or convention about
     how code or processes should work. These belong in the conventions
     DB via `tusk conventions add`.
   - **Narrative/reference** — multi-step procedures or explanatory
     context. These belong as a patch to a prompt file
     (`.codex/prompts/<name>.md`) or to the project's agent doc
     (`AGENTS.md` / `CLAUDE.md`).

2. **If the finding is rule-like** — propose adding a convention via
   `tusk conventions add`:
   - Draft the exact convention text (one concise sentence) and a
     comma-separated list of relevant topic tags.
   - Present the proposal with three options (`approve` / `defer` /
     `skip`). `approve` runs the command now and skips task creation
     for that finding; `defer` includes the proposed command in the
     task description; `skip` proceeds to normal task creation.

3. **If the finding is narrative/reference** — identify a target file
   (a prompt name in `.codex/prompts/` or the project agent doc),
   produce a concrete proposed edit, and present it with the same three
   options. `approve` applies the edit now; `defer` includes the diff
   in the task description; `skip` proceeds to normal task creation.

### LR-2b: Apply Lint Rules Inline (only if Category D findings exist)

For each lint rule finding:

1. Present the proposed rule and command:
   ```bash
   tusk lint-rule add '<pattern>' '<file_glob>' '<message>'
   ```
   Ask for approval. The bar is high — only proceed if you observed an
   actual mistake that a grep rule would have caught.

2. **If approved**, run the command. **Success**: note the rule ID; do
   **not** create a task. **Error or unavailable**: fall back to task
   creation.

3. **If declined or fallback**:
   ```bash
   tusk task-insert "Add lint rule: <short description>" \
     "Run: tusk lint-rule add '<pattern>' '<file_glob>' '<message>'" \
     --priority "Low" --task-type "<task_type>" --complexity "XS" \
     --criteria "tusk lint-rule add has been run with the specified pattern, glob, and message"
   ```

### LR-3: Report

The `tusk.md` flow already printed the canonical task summary block
(`tusk task-summary <id> --format markdown`) immediately before
invoking this retro, so the user has already seen the
identity/cost/duration/diff/criteria rollup for `RETRO_TASK_ID`. Do
**not** re-emit that block — start directly with the retrospective
findings:

```markdown
## Retrospective Complete (Lightweight)

**Session**: <what was accomplished>
**Findings**: X total (by category)
**Created**: N tasks (#id, #id)
**GitHub issues filed**: N (tusk-issues routed via tusk report-issue — omit line if zero)
**Lint rules**: K applied inline, M deferred as tasks
**Skipped**: M duplicates
```

If `themes` from Step 0a was non-empty, append a "Recurring themes
flagged" line listing the matched themes.

Then show the current backlog:

```bash
tusk -header -column "SELECT id, summary, priority, domain, task_type, status FROM tasks WHERE status = 'To Do' ORDER BY priority_score DESC, id"
```

### LR-3a: Record Approved Findings

Before closing the skill run, write one `retro_findings` row per
**approved** finding (task created, issue filed, lint rule added,
convention added, or prompt-patched inline). Skipped/duplicate findings
are **not** recorded — only actioned ones feed the cross-retro signal.
For each approved finding:

```bash
tusk retro-finding add \
  --skill-run-id <run_id> \
  --category '<category>' \
  --summary '<one-line summary>' \
  [--task-id $RETRO_TASK_ID] \
  [--action-taken '<action_taken>']
```

`<action_taken>` vocabulary (omit `--action-taken` if none fit):
- `task:TASK-<id>` — new task created via `tusk task-insert`
- `issue:<url>` — GitHub issue filed via `tusk report-issue`
- `lint:<id>` — lint rule added via `tusk lint-rule add`
- `convention:<id>` — convention added via `tusk conventions add`
- `prompt-patch:<file>` — inline edit applied to a prompt file or
  agent doc
- `documented` — recorded without a concrete action

**Omit** `--task-id` entirely when `RETRO_TASK_ID` is null.

Finally, close out the retro skill-run:

```bash
tusk skill-run finish <run_id>
```

**End of lightweight retro.** Do not continue to the Full Retro path.

---

## Full Retro (M/L/XL/NULL tasks)

For larger tasks, run a deeper retro that adds subsumption analysis,
known-gaps reporting, and dependency proposals.

### FR-1: Pull Session Context

The session signals you need are already in `signals` from Step 0a —
specifically `review_themes`, `deferred_review_comments`,
`skipped_criteria`, `tool_call_outliers`, `unconsumed_next_steps`,
`reopen_count`, and `rework_chain`.

For deeper code review of the just-closed task, run:

```bash
git log --oneline $(git merge-base HEAD $(tusk git-default-branch))..HEAD
```

Skim the diff range with `git diff <merge_base>..HEAD` if needed.

### FR-2: Review & Categorize (deeper)

Use the same five categories as LR-1 (Process improvements, Tangential
issues, Follow-up work, Lint Rules, Debugging Velocity), but expand
analysis with:

- **Subsumption** — Did this task quietly absorb work originally
  scoped to other open tasks? If so, list those tasks for closure as
  duplicates. Use `signals.rework_chain` to detect upstream tasks
  whose criteria this session also satisfied.
- **Reopen / rework signals** — If `signals.reopen_count > 0` or
  `rework_chain.fixes` is non-empty, treat the underlying root cause
  as a primary finding.

When `themes` from Step 0a is non-empty, flag findings whose category
matches a recurring theme.

### FR-3: Classify and Plan (LR-1b + LR-2 routing)

Apply the LR-1b classification (tusk-issue vs. project-issue) and the
LR-2 routing rules (file GitHub issues for tusk-issues, insert tasks
for project-issues, with LR-2a convention/prompt-patch logic for
Category A/E and LR-2b inline lint rule application for Category D).

### FR-4: Known Gaps at Close

Render this section *only* when one or both of the following are true:

- `signals.skipped_criteria` is non-empty.
- `signals.unconsumed_next_steps` is non-empty *and* the session output
  did not address those next steps.

Format:

```markdown
### Known Gaps at Close
| Kind | Detail | Disposition |
|------|--------|-------------|
| Skipped criterion | <criterion text> | <reason> |
| Unconsumed next-step | <next_steps text> | confirmed / ambiguous |
```

Prompt the user for a yes/no on any ambiguous next-step matches before
including them.

If both arrays are empty, omit this section entirely.

### FR-5: Dependency Proposals

If you created two or more tasks in FR-3, scan for ordering:

- **blocks** — A's deliverable must exist before B can start (hard
  prerequisite).
- **contingent** — B is worth doing only if A's outcome warrants it.

Present proposals to the user. For each confirmed:

```bash
tusk deps add <task_id> <depends_on_id> [--type blocks|contingent]
```

### FR-6: Final Report and Findings Record

The `tusk.md` flow already printed the canonical task summary, so start
directly with findings:

```markdown
## Retrospective Complete (Full)

**Session**: <what was accomplished>
**Findings**: X total (by category)
**Subsumed tasks closed**: N (#id, #id) — only if any were subsumed
**Created**: N tasks (#id, #id)
**GitHub issues filed**: N — omit if zero
**Lint rules**: K applied inline, M deferred as tasks
**Conventions added**: P
**Prompt patches applied**: Q
**Dependencies added**: R — omit if zero
```

Show the current backlog:

```bash
tusk -header -column "SELECT id, summary, priority, domain, task_type, status FROM tasks WHERE status = 'To Do' ORDER BY priority_score DESC, id"
```

Record approved findings via `tusk retro-finding add` (same shape as
LR-3a — one row per approved finding).

Finally, close the skill run:

```bash
tusk skill-run finish <run_id>
```
