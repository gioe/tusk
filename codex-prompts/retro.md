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

`RETRO_TASK_ID` is the just-closed task this retro is reviewing — its
`complexity` becomes `RETRO_COMPLEXITY`. Resolve in this order
(issue #805, original incident: with parallel worktrees finalizing
tasks within seconds of each other, the most-recent-Done heuristic
returns whichever sibling closed last — not the task `tusk.md` just
finalized):

1. **Argv-supplied task id** — when this prompt was invoked as
   `retro.md <task_id>` (the normal handoff from `tusk.md` Step 12 and
   `address-issue.md` Step 10), use that id directly. Confirm with
   `tusk task-get <task_id>` and read its `complexity` field from
   `.task.complexity`, for example:
   `tusk task-get <task_id> | jq -r .task.complexity`.
2. **Most-recent-Done fallback** — only when no argv was passed
   (stand-alone retro invocations typed by the user). Use the
   `ORDER BY` heuristic below.

```bash
# Fallback only — skip if RETRO_TASK_ID was supplied via argv:
tusk "SELECT id, complexity FROM tasks WHERE status = 'Done' ORDER BY updated_at DESC LIMIT 1"
```

Store the resolved `id` as `RETRO_TASK_ID` and `complexity` as
`RETRO_COMPLEXITY`. If both paths yield nothing (no Done tasks exist
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
  `skipped_criteria`, `tool_call_outliers`, `tool_errors`,
  `context_health`, `unconsumed_next_steps`.
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

- **Category A** — Tusk workflow failures: failures, confusing
  behavior, missing safeguards, or broken handoffs in tusk itself that
  should be filed as tusk issues. This includes CLI, prompt, hook, DB,
  install, review, merge, task lifecycle, or automation behavior that
  made the task harder or less reliable.
- **Category B** — Context-window tangents: issues noticed in the
  context that was pulled into the session, but unrelated to the work
  just shipped. Use this for bugs, tech debt, architectural concerns,
  stale patterns, or suspicious nearby behavior worth addressing later.
  Do not use this for unfinished scoped work (Category C) or docs that
  need updating because of the shipped change (Category D).
- **Category C** — Task-adjacent follow-up: issues noticed in the
  context window that are related to the task or changed area, but were
  not part of what just shipped. Use this for adjacent edge cases,
  parity work, secondary workflows, or deferred decisions that should be
  addressed later. Category B is for unrelated context-window issues.
- **Category D** — Project Documentation Updates: project docs that
  should change because of what just shipped. Inspect the task summary
  and diff for changed commands, config keys, workflows, schemas,
  prompt/skill behavior, install behavior, user-facing output, or
  operational gotchas. Check whether the relevant durable docs were
  updated in the same task: `CLAUDE.md`/`AGENTS.md`, `README.md`,
  `docs/`, `.codex/prompts/`, and distributed `skills/` files. If docs
  are stale or missing, create a concrete doc follow-up or propose an
  inline doc patch. If behavior changed and no docs need updates,
  explicitly mark this category empty with the reason.

Analyze the full conversation context using these categories. Also run
these two cross-cutting checks after categorizing findings:

- **Debugging velocity lens** — if the session involved fixing a bug or
  diagnosing unexpected behavior, ask what would have reduced
  time-to-root-cause: a test, log, trace, command, tusk safeguard,
  clearer handoff, or documentation. Classify any resulting finding
  into Category A, B, C, or D; do not create a separate debugging
  category.
- **Mechanical guard action route** — if any finding describes an actual
  mistake that can be prevented by a concrete grep-detectable pattern,
  mark its proposed action as "add lint rule" and capture the pattern,
  file glob, and message. Do not use this for general advice or style
  preferences.

For each default category, explicitly record `none` or list the
findings. This keeps the retro from silently skipping a bucket.

If **all categories are empty**, run `tusk skill-run cancel <run_id>`,
report "Clean session — no findings" and stop.

When `themes` from Step 0a is non-empty, flag any finding whose
category matches a recurring theme — note "theme `<name>` recurring —
seen N times in last 30 days" next to that finding in the report.

### LR-1b: Classify Each Finding

For each finding, first choose the smallest durable unit that matches
it:

- **Task** — shippable backlog work that needs its own branch,
  worktree, review, and merge.
- **Criterion** — an observable completion condition that belongs on an
  existing open task.
- **Context atom** — durable memory that improves future handoff but is
  not shippable work: an assumption, question, risk, decision, entry
  point, or compact memory.

For task findings, determine whether it is a **tusk-issue** or a
**project-issue**:

- **tusk-issue** — a bug, limitation, or improvement in tusk itself:
  the CLI, a prompt, DB schema, or installed tooling.
- **project-issue** — specific to the current project: its code,
  architecture, conventions, or processes.

Label each finding with its durable unit and, for task findings, its
classification. This drives the routing in LR-2.

Category A findings are always **tusk-issues**. Category D findings are
normally **project-issues** unless the missing documentation is in tusk's
distributed docs/prompts/skills.

### LR-2: Create Tasks / File Issues (only if findings exist)

1. Compare each finding against the backlog from Step 0b for semantic
   overlap. Drop any already covered.

2. Run heuristic dupe check on surviving findings:
   ```bash
   tusk dupes check "<proposed summary>"
   ```

3. Present findings and proposed actions in a table (include the
   durable unit and, for task findings, the classification from LR-1b).
   Wait for explicit user approval before acting.

4. For each approved finding, route based on its LR-1b durable unit:

   **criteria** — add the finding to the best matching open task:
   ```bash
   tusk criteria add <task_id> "<criterion>"
   ```
   Do **not** create a new task for this finding.

   **context atoms** — write through the first-class context CLI:
   ```bash
   tusk context add <task_id> --type risk --content "<finding summary>" --source retro
   tusk context resolve <context_item_id>
   tusk context supersede <context_item_id>
   ```
   Choose `memory`, `assumption`, `question`, `risk`, `decision`, or
   `entry_point` as narrowly as possible. Use `resolve` when the
   finding closes an active question/risk/assumption; use `supersede`
   when the finding replaces stale context. Do **not** use direct SQL
   for context atoms. Context atoms should preserve durable memory
   without inflating the task backlog.

   **tasks** — route based on LR-1b classification:

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

   **project-issues** — If the approved finding's proposed action is
   "add convention", or if it is a **Category D** documentation finding,
   follow **LR-2a** below before inserting tasks. For all other
   project-issue findings, insert tasks now:
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

### LR-2a: Inline Convention / Prompt-Doc Actions

Before creating tasks for routed project-issue findings, check whether
the approved action can be applied inline as a convention or
documentation patch.

For each approved project-issue finding routed here:

1. **Classify the finding as rule-like or narrative:**
   - **Rule-like** — a single heuristic, invariant, or convention about
     how code or processes should work. These belong in the conventions
     DB via `tusk conventions add`.
   - **Narrative/reference** — multi-step procedures or explanatory
     context. These belong as a patch to a prompt file
     (`.codex/prompts/<name>.md`), the project's agent doc
     (`AGENTS.md` / `CLAUDE.md`), `README.md`, or a file under `docs/`.

2. **If the finding is rule-like** — propose adding a convention via
   `tusk conventions add`:
   - Draft the exact convention text (one concise sentence) and a
     comma-separated list of relevant topic tags.
   - Present the proposal with three options (`approve` / `defer` /
     `skip`). `approve` runs the command now and skips task creation
     for that finding; `defer` includes the proposed command in the
     task description; `skip` proceeds to normal task creation.

3. **If the finding is narrative/reference** — identify a target file
   (a prompt name in `.codex/prompts/`, the project agent doc,
   `README.md`, or a specific file under `docs/`), produce a concrete
   proposed edit, and present it with the same three options. `approve`
   applies the edit now; `defer` includes the diff in the task
   description; `skip` proceeds to normal task creation.

### LR-2b: Apply Lint Rules Inline (only if lint-rule action candidates exist)

For each lint-rule action candidate:

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
**Criteria added**: N — omit if zero
**Context atoms updated**: N added, R resolved, S superseded — omit if all zero
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
criterion added, context atom updated, convention added,
prompt-patched inline, or doc-patched inline).
Skipped/duplicate findings are **not** recorded — only actioned ones
feed the cross-retro signal.
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
- `criterion:<id>` — acceptance criterion added via `tusk criteria add`
- `context:<id>` — context atom added, resolved, or superseded via
  `tusk context`
- `issue:<url>` — GitHub issue filed via `tusk report-issue`
- `lint:<id>` — lint rule added via `tusk lint-rule add`
- `convention:<id>` — convention added via `tusk conventions add`
- `prompt-patch:<file>` — inline edit applied to a prompt file or
  agent doc
- `doc-patch:<file>` — inline edit applied to README.md or a file under
  docs/
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
specifically `review_themes`, `skipped_criteria`, `tool_call_outliers`,
`tool_errors`, `context_health`, `unconsumed_next_steps`,
`reopen_count`, and `rework_chain`.

For deeper code review of the just-closed task, run:

```bash
git log --oneline $(git merge-base HEAD $(tusk git-default-branch))..HEAD
```

Skim the diff range with `git diff <merge_base>..HEAD` if needed.

### FR-2: Review & Categorize (deeper)

Use the same four categories as LR-1 (Tusk workflow failures,
Context-window tangents, Task-adjacent follow-up, Project Documentation
Updates), but expand analysis with:

- **Subsumption** — Did this task quietly absorb work originally
  scoped to other open tasks? If so, list those tasks for closure as
  duplicates. Use `signals.rework_chain` to detect upstream tasks
  whose criteria this session also satisfied.
- **Reopen / rework signals** — If `signals.reopen_count > 0` or
  `rework_chain.fixes` is non-empty, treat the underlying root cause
  as a primary finding.
- **Documentation drift** — Inspect the task summary, commit list, and
  diff for behavior future task runs or users need to know. Verify the
  durable project docs were updated in the same task
  (`CLAUDE.md`/`AGENTS.md`, `README.md`, `docs/`, `.codex/prompts/`,
  and distributed `skills/`). Missing or stale docs become Category D
  findings with a concrete target file and acceptance criteria.
- **Debugging velocity lens** — if this was a bug or diagnosis task,
  ask what would have reduced time-to-root-cause and classify the
  resulting finding into Category A, B, C, or D.
- **Mechanical guard action route** — if any finding can be prevented by
  a concrete grep-detectable pattern, attach an "add lint rule" proposed
  action with the pattern, file glob, and message.
- **Context atom route** — for every finding, ask whether the smallest
  durable unit is a task, a criterion on an existing task, or a context
  atom. Use a context atom for durable memory that should help a future
  handoff but is not shippable backlog work.
- **Context snapshot health** — review `signals.context_health`.
  Active risks/questions/assumptions, missing entry points, and
  resolved/superseded candidates should become context updates by
  default; promote them to tasks only when they require shippable work.

When `themes` from Step 0a is non-empty, flag findings whose category
matches a recurring theme.

### FR-3: Classify and Plan (LR-1b + LR-2 routing)

Apply the LR-1b durable-unit classification (task vs. criterion vs.
context atom). For task findings, also classify tusk-issue vs.
project-issue and apply the LR-2 routing rules (file GitHub issues for
tusk-issues, insert tasks for project-issues, with LR-2a
convention/prompt-doc patch logic for convention actions and Category D
findings, and LR-2b inline lint rule application for lint-rule action
candidates). For criterion and context-atom findings, use the LR-2
commands directly and do not create tasks.

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

### FR-4a: Context Snapshot

Render this section only when `signals.context_health.active_items` is
non-empty, `signals.context_health.inactive_items` is non-empty, or
`signals.context_health.missing_entry_points` is true.

```markdown
### Context Snapshot
> Durable memory — these rows are context atoms, not backlog work.
> Promote to tasks only when the item requires a shippable change.

**Missing entry points**: yes/no

| ID | Type | Status/Source | Content |
|----|------|---------------|---------|
```

Use `tusk context add`, `tusk context resolve`, or `tusk context
supersede` for approved context updates. Do not query or update
`task_context_items` directly.

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
**Criteria added**: N — omit if zero
**Context atoms updated**: N added, R resolved, S superseded — omit if all zero
**GitHub issues filed**: N — omit if zero
**Lint rules**: K applied inline, M deferred as tasks
**Conventions added**: P
**Prompt/doc patches applied**: Q
**Dependencies added**: R — omit if zero
```

Show the current backlog:

```bash
tusk -header -column "SELECT id, summary, priority, domain, task_type, status FROM tasks WHERE status = 'To Do' ORDER BY priority_score DESC, id"
```

Record approved findings via `tusk retro-finding add` (same shape as
LR-3a — one row per approved finding). Use `criterion:<id>` for
criteria added through `tusk criteria add` and `context:<id>` for
context atoms added, resolved, or superseded through `tusk context`.

Finally, close the skill run:

```bash
tusk skill-run finish <run_id>
```
