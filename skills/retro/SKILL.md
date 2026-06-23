---
name: retro
description: Review the current session, surface process improvements and tangential issues, and create follow-up tasks
allowed-tools: Bash, Read, Edit
---

# Retrospective Skill

Reviews the current conversation history to capture process learnings, instruction improvements, and tangential issues. Creates structured follow-up tasks so nothing falls through the cracks.

> Use `/create-task` for task creation — handles decomposition, deduplication, criteria, and deps. Use `tusk task-insert` only for bulk/automated inserts.

## Step 0: Setup

`RETRO_TASK_ID` identifies the single just-closed task this retro is reviewing. Resolve it in this order (issue #805, original incident: with parallel worktrees finalizing tasks within seconds of each other, the most-recent-Done heuristic returns whichever sibling closed last — not the task `/tusk` just finalized):

1. **Argv-supplied task id** — when the skill was invoked as `/retro <task_id>` (the normal handoff from `/tusk` Step 12 and `/address-issue` Step 10), use that id directly. Confirm with `tusk task-get <task_id>` and read its `complexity` field from the returned JSON at `.task.complexity` (for example: `tusk task-get <task_id> | jq -r .task.complexity`). This is the authoritative path for handoffs.
2. **Most-recent-Done fallback** — only when no argv was passed (stand-alone `/retro` invocations typed by the user). Use the ORDER BY heuristic below.

```bash
# Fallback only — skip if RETRO_TASK_ID was supplied via argv:
tusk "SELECT id, complexity FROM tasks WHERE status = 'Done' ORDER BY updated_at DESC LIMIT 1"
tusk setup
```

Parse the JSON from `tusk setup`: use `config` for metadata assignment and `backlog` for duplicate comparison. (Run `tusk setup` regardless of which path resolved `RETRO_TASK_ID` — config + backlog are always needed.)

Store the resolved id as `RETRO_TASK_ID`. Then start cost tracking:

```bash
tusk skill-run start retro --task-id $RETRO_TASK_ID
```

This prints `{"run_id": N, "started_at": "...", "task_id": N}`. Capture `run_id` — it's referenced by every exit path below. If the query returned no rows (no Done tasks exist yet), omit `--task-id`:

```bash
tusk skill-run start retro
```

> **Early-exit cleanup:** If any step below causes the retro to stop before reaching the final report (LR-3 for lightweight, Step 6 for full), first call `tusk skill-run cancel <run_id>` to close the open row, then stop. Otherwise the row lingers as `(open)` in `tusk skill-run list` forever.

## Step 0b: Cross-retro theme check

Fetch themes recurring 3+ times across approved findings in the last 30 days so this retro can see patterns that any single retrospective misses:

```bash
tusk retro-themes --window-days 30 --min-recurrence 3
```

The output is pre-aggregated `{theme, count}` tuples — **do not** issue separate SQL queries against `retro_findings`. All cross-retro aggregation belongs in SQL behind `tusk retro-themes`; `/retro` consumes only the tuple stream.

If `themes` is empty, skip — the current session stands alone. If any tuple is returned, store the list as `$RECURRING_THEMES` and use it in LR-1 (or Step 3 in FULL-RETRO) to flag recurrences: when this session surfaces a finding whose summary text contains a recurring theme, note the recurrence (e.g. `theme 'manifest' recurring — seen N times in last 30 days`) next to that finding in the report. Themes are normalized topic terms extracted from `retro_findings.summary` (issue #551), not single-letter category codes.

- **XS or S** → follow the **Lightweight Retro** path below
- **M, L, XL, or NULL** → read the full retro guide:
  ```
  Read file: <base_directory>/FULL-RETRO.md
  ```
  Then follow Steps 1–6 from that file. Do not continue below.

---

## Lightweight Retro (XS/S tasks)

Streamlined retro for small tasks. Skips subsumption analysis and dependency proposals.

### LR-1: Review & Categorize

**Read mid-task jots first.** If `RETRO_TASK_ID` is set, fetch any friction notes captured during the task via `tusk jot`:

```bash
tusk jots --task-id $RETRO_TASK_ID
```

The output is an array of `{id, skill_run_id, task_id, category, note, file_hint, skill_hint, created_at}` rows. Each jot is a **pre-classified finding candidate** captured at the moment of friction — treat its `category` as a strong hint when bucketing into the categories below, and quote the `note` verbatim in the finding's summary. Jots are the highest-fidelity input to retro because they were not reconstructed from memory at close time. Empty array → no jots were filed; proceed using conversation context alone.

**Read scope-quality signals next.** Fetch the task's declared scope (TASK-471):

```bash
tusk scope list $RETRO_TASK_ID
```

The output is an array of `{id, task_id, pattern, source, reason, locked_at, locked_by, created_at}` rows. Inspect for the following signals — they are independent of jots and may surface findings the operator never wrote down:

- **`expanded_mid_task` rows** — each one is the operator answering "I had to grow scope and here's why". Quote the `pattern` and `reason` verbatim. If multiple expansions cite the same root cause (e.g. "missed during decomposition"), that's a Category A finding when tusk's task-creation or scope workflow failed to capture the real work. If expansions cite genuine exploration discoveries, no finding — scope growth from new information is healthy.
- **`auto_derived`-only tasks that ended up needing `TUSK_SCOPE_GUARD_BYPASS=1`** — the legacy hint cache wasn't precise enough. Category A: tusk's scope workflow needs a better safeguard or handoff.
- **`unbounded` rows on tasks that turned out to touch < 5 files** — the operator opted out of the guard for a task that didn't actually need it. Category A if tusk made declaring scope too hard or unclear.
- **Locked-but-still-grew tasks** — a `locked_at` timestamp followed by a later `expanded_mid_task` row means the lock was an aspirational ceremony, not a hard checkpoint. Category A: consider whether `tusk scope add` should refuse after lock, or whether lock should require an explicit `--unlock` for further growth.

Empty array on a task that has commits → the operator bypassed the guard for the entire session, or the task was created before migration 73 and scope was never declared. The former is a Category A finding; the latter is expected for legacy work and produces no finding.

**Check for custom focus areas first.** Attempt to read `<base_directory>/FOCUS.md`.
- If the file exists: use the categories defined in it for the analysis below.
- If the file does not exist: use the default categories:
  - **Category A**: Tusk workflow failures — failures, confusing behavior, missing safeguards, or broken handoffs in tusk itself that should be filed as tusk issues. This includes CLI, skill, prompt, hook, DB, install, review, merge, task lifecycle, or automation behavior that made the task harder or less reliable.
  - **Category B**: Context-window tangents — issues noticed in the context that was pulled into the session, but unrelated to the work just shipped. Use this for bugs, tech debt, architectural concerns, stale patterns, or suspicious nearby behavior worth addressing later. Do not use this for unfinished scoped work (Category C) or docs that need updating because of the shipped change (Category D).
  - **Category C**: Task-adjacent follow-up — issues noticed in the context window that are related to the task or changed area, but were not part of what just shipped. Use this for adjacent edge cases, parity work, secondary workflows, or deferred decisions that should be addressed later. Category B is for unrelated context-window issues.
  - **Category D**: Project Documentation Updates — project documentation that should change because of what just shipped. Inspect the task summary and diff for changed commands, config keys, workflows, schemas, prompt/skill behavior, install behavior, user-facing output, or operational gotchas. Check whether the relevant durable docs were updated in the same task: `CLAUDE.md`/`AGENTS.md`, `README.md`, `docs/`, `.codex/prompts/`, and distributed `skills/` files. If the docs are stale or missing, create a concrete doc follow-up or propose an inline doc patch. If behavior changed and no docs need updates, explicitly mark this category empty with the reason.

Analyze the full conversation context using the resolved categories. Also run these two cross-cutting checks after categorizing findings:

- **Debugging velocity lens** — if the session involved fixing a bug or diagnosing unexpected behavior, ask what would have reduced time-to-root-cause: a test, log, trace, command, tusk safeguard, clearer handoff, or documentation. Classify any resulting finding into Category A, B, C, or D; do not create a separate debugging category.
- **Mechanical guard action route** — if any finding describes an actual mistake that can be prevented by a concrete grep-detectable pattern, mark its proposed action as "add lint rule" and capture the pattern, file glob, and message. Do not use this for general advice or style preferences.

For each default category, explicitly record `none` or list the findings. This keeps the retro from silently skipping a bucket.

If **all categories are empty**, run `tusk skill-run cancel <run_id>`, report "Clean session — no findings" and stop. (Config and backlog were already fetched in Step 0 — no additional work needed.)

### LR-1b: Classify Each Finding

For each finding, first choose the smallest durable unit that matches it:

- **Task** — shippable backlog work that needs its own branch/worktree/review.
- **Criterion** — an observable completion condition that belongs on an existing open task; use `tusk criteria add <task_id> "<criterion>"` rather than creating a sibling task.
- **Context atom** — durable memory that improves future handoff but is not shippable work: an assumption, question, risk, decision, entry point, or compact memory. Context atoms must not inflate the task backlog.

For findings whose durable unit is a task, determine whether it is a **tusk-issue** or a **project-issue**:

- **tusk-issue** — a bug, limitation, or improvement in tusk itself: the CLI, a skill, DB schema, or installed tooling (e.g., a skill instruction is confusing, a `tusk` command misbehaves, a missing feature in the tool)
- **project-issue** — specific to the current project: its code, architecture, conventions, or processes

Label each finding with its durable unit and, for task findings, its classification. This drives the routing in LR-2.

Category A findings are always **tusk-issues**. Category D findings are normally **project-issues** unless the missing documentation is in tusk's distributed docs/prompts/skills.

### LR-2: Create Tasks / File Issues (only if findings exist)

1. Compare each finding against the backlog for semantic overlap (use `backlog` from Step 0). Drop any already covered.

2. Run heuristic dupe check on surviving findings:
   ```bash
   tusk dupes check "<proposed summary>"
   ```

3. Present findings and proposed actions in a table (include the durable unit and, for task findings, the classification from LR-1b). Wait for explicit user approval before acting.

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
   Choose `memory`, `assumption`, `question`, `risk`, `decision`, or `entry_point` as narrowly as possible. Use `resolve` when the finding closes an active question/risk/assumption; use `supersede` when the finding replaces stale context. Do **not** use direct SQL for context atoms.

   **tasks** — route based on LR-1b classification:

   **tusk-issues** — file a GitHub issue via:
   ```bash
   tusk report-issue --title "<finding title>" --cluster triage-needed --context "<finding description>"
   ```
   Pick the most specific `cluster:<name>` label that fits the finding — the CLI accepts any cluster name currently labelled on the repo, so new clusters work immediately. Run `gh label list --repo gioe/tusk --search "cluster:"` to see the current set. Use `triage-needed` only as the fallback. Do **not** call `tusk task-insert` for tusk-issues. Track the count of issues filed for LR-3.

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
   Note in LR-3 that the issue was tracked as a local task rather than filed on GitHub.

   **project-issues** — If the approved finding's proposed action is "add convention", or if it is a **Category D** documentation finding, follow **LR-2a** below before inserting tasks. For all other project-issue findings, insert tasks now:
   ```bash
   tusk task-insert "<summary>" "<description>" --priority "<priority>" --domain "<domain>" --task-type "<task_type>" --assignee "<assignee>" --complexity "<complexity>" \
     --criteria "<criterion 1>" [--criteria "<criterion 2>" ...]
   ```
   Always include at least one `--criteria` flag — derive 1–3 concrete acceptance criteria from the task description. Omit `--domain` or `--assignee` entirely if the value is NULL/empty. Exit code 1 means duplicate — skip. Skip subsumption and dependency proposals.

   **Auto-prioritize deferred skill-patch tasks.** When a deferred **skill/doc patch** finding (step 3f above — the task description carries a proposed diff against a skill/agent/doc file) is inserted, it would otherwise land at the unmodified default priority and rot in the backlog. Immediately after the `task-insert` returns the new task ID, derive a priority from the task's retro-signals (reopen counts, rework chains, recurring review themes — higher counts yield higher priority) and apply it:
   ```bash
   tusk skill-patch-priority <new_task_id> --apply
   ```
   This is a no-op for a brand-new task with no rework history (it stays at default), but lifts the priority when the underlying skill/doc has a history of reopens or fix-chains. Only run it for skill/doc-patch follow-ups, not for every project-issue task.

### LR-2a: Inline Convention/Doc Actions

Before creating tasks for project-issue findings, check whether the approved action can be applied inline as a convention or documentation patch.

Initialize an empty list `$AUTO_APPLIED` for the LR-3 summary — auto-apply gate hits in step 3 below append one line per applied edit.

For each approved project-issue finding routed here:

1. **Classify the finding as rule-like or narrative:**
   - **Rule-like**: a single heuristic, invariant, or convention about how code or processes should work — e.g., "always quote file paths in zsh", "always pass `encoding='utf-8'` to `subprocess.run(text=True)`". These belong in the conventions DB via `tusk conventions add`.
   - **Narrative/reference**: multi-step procedures, workflow descriptions, explanatory context, or anything that requires more than one sentence to express correctly. These belong as a patch to a skill file, agent doc, README, or file under `docs/`.

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
      > **defer** — create a task with this command included in the description
      > **skip** — create a generic task as usual

   c. **If approved**: run the command now using Bash. Do **not** create a task for this finding.
   d. **If deferred**: include the proposed command verbatim in the task description when calling `tusk task-insert`.
   e. **If skipped**: proceed to normal task creation (step 4 in LR-2).

3. **If the finding is narrative/reference** — identify a target file:
   - A skill name matching a directory in `.claude/skills/` (list them with `ls .claude/skills/`)
   - The string `CLAUDE.md` or `AGENTS.md`
   - `README.md`
   - A specific file under `docs/`

   **If a target file is identified**:
   a. Read the file (`Read .claude/skills/<name>/SKILL.md`, `Read CLAUDE.md`, `Read AGENTS.md`, `Read README.md`, or the specific `docs/<path>.md`)
   b. Produce a **concrete proposed edit** — the exact text to add, change, or remove. Show the specific diff, not a vague description.

   c. **Auto-apply gate (only for skill-frontmatter edits).** Before showing the three-option prompt, read the `retro` config once:

      ```bash
      tusk config retro
      ```

      The command prints the `retro` JSON object (or exits non-zero / prints nothing when the key is unset). If `retro.auto_apply` is not exactly `true` — i.e. the key is missing, the value is `false`/`null`, or `tusk config retro` printed nothing — **skip this gate entirely** and proceed to step 3d (three-option prompt). Behavior must match the pre-auto-apply flow exactly when the feature is disabled. When `retro.auto_apply` is `true`, also read `retro.auto_apply_max_chars` (default `200` if unset) for the size budget below.

      If `retro.auto_apply` is `true`, the proposed edit qualifies for auto-apply only when **ALL** of the following hold:

      - **Frontmatter-only**: every changed line lies inside the YAML frontmatter block (between the opening `---` and closing `---` at the top of `.claude/skills/<name>/SKILL.md`), and each changed line is either a `description:` line or a `#`-prefixed comment line. Body changes (anything below the closing `---`), `name:` / `allowed-tools:` / other frontmatter fields, and agent-doc/project-doc edits never qualify.
      - **Size budget**: the total character count of the diff (sum of `old_string` length + `new_string` length, or for additions just the `new_string` length) is strictly less than `retro.auto_apply_max_chars` (default 200).
      - **Additive or character-level**: either (a) the change is a pure addition — `new_string` extends `old_string` with appended content and contains no deletions — or (b) the change is character-level on a single line — exactly one frontmatter line is modified, and the modification only inserts or replaces characters within that line (no full-line removals, no multi-line removals).

      If **any** condition fails, fall through to step 3d (three-option prompt). Do not partially auto-apply.

      If **all** conditions hold and `retro.auto_apply` is enabled, **apply the edit immediately using the Edit tool — skip the three-option prompt entirely**. Record a one-line entry in `$AUTO_APPLIED` (file path + brief description, one entry per line) for the LR-3 summary, and do **not** create a task for this finding. Proceed to the next finding.

   d. Otherwise, present the patch with three options:

      > **Skill/Doc Patch Proposal** — [finding title]
      > File: `<target file>`
      >
      > ```diff
      > - [existing text to replace]
      > + [replacement text]
      > ```
      >
      > **approve** — apply the edit now (no task created for this finding)
      > **defer** — create a task with this diff included in the description
      > **skip** — create a generic task as usual

   e. **If approved**: apply the edit in-session using the Edit tool. Do **not** create a task for this finding.
   f. **If deferred**: include the proposed diff verbatim in the task description when calling `tusk task-insert`.
   g. **If skipped, or if no target file was identified**: proceed to normal task creation (step 4 in LR-2).

### LR-2b: Apply Lint Rules Inline (only if lint-rule action candidates exist)

Apply this step if any approved finding has a proposed "add lint rule" action. With custom FOCUS.md categories, also apply this step for entries in a "Lint Rules" section.

The bar is high — only proceed if you observed an **actual mistake** that a grep rule would have caught. Do not apply lint rules for general advice.

For each grep-detectable anti-pattern you surfaced, **emit a `tusk lint-rule propose` call** rather than instructing the operator to run `tusk lint-rule add` by hand. `propose` stages the rule **advisory** — its hits warn but never gate `tusk lint`/`commit`/`merge` until someone runs `tusk lint-rule promote <id>` once the pattern is observed to hold — and records provenance back to the originating retro finding via `--finding-id`. This keeps a newly-proposed rule from blocking work before it has been validated.

1. **Present the proposed rule** — show the exact command and ask for approval:

   > Found lint rule candidate: [finding description]
   > Command: `tusk lint-rule propose '<pattern>' '<file_glob>' '<message>'`
   > Stage this advisory rule now? (Reversible with `tusk lint-rule remove <id>`; promote later with `tusk lint-rule promote <id>`.)

2. **If the user approves** — run the command immediately. If you have already recorded the originating finding in LR-3a, pass its id so the proposed rule carries provenance:
   ```bash
   tusk lint-rule propose '<pattern>' '<file_glob>' '<message>' [--finding-id <finding_id>]
   ```
   - **Success**: note the rule ID returned. **Do not create a task** for this finding. Record the action as `lint:<id>` in LR-3a.
   - **Error or unavailable**: fall back to task creation (step 3).

3. **If the user declines**, or **if inline application fails**, create a task as a fallback:
   ```bash
   tusk task-insert "Add lint rule: <short description>" \
     "Run: tusk lint-rule propose '<pattern>' '<file_glob>' '<message>'" \
     --priority "Low" --task-type "<task_type>" --complexity "XS" \
     --criteria "tusk lint-rule propose has been run with the specified pattern, glob, and message"
   ```

For `<task_type>`: use the project's config `task_types` array (already fetched via `tusk setup` in Step 0). Pick the entry that best fits a maintenance/tooling task (e.g., `maintenance`, `chore`, `tech-debt`, `infra` — whatever is closest in your project's list). If no entry is a clear fit, omit `--task-type` entirely.

Fill in `<pattern>` (grep regex), `<file_glob>` (e.g., `*.md` or `bin/tusk-*.py`), and `<message>` (human-readable warning) with the specific values from your finding.

### LR-3: Report

The /tusk skill already printed the task summary block (`tusk task-summary <id> --format markdown`) immediately before invoking /retro, so the user has already seen the canonical identity/cost/duration/diff/criteria rollup for the just-closed task. Do **not** re-emit that block here — start directly with the retrospective findings so the two sections read as one continuous report.

```markdown
## Retrospective Complete (Lightweight)

**Session**: <what was accomplished>
**Findings**: X total (by category — use resolved category names)
**Created**: N tasks (#id, #id)
**Criteria added**: N (omit line if zero)
**Context atoms updated**: N added, R resolved, S superseded (omit line if all zero)
**GitHub issues filed**: N (tusk-issues routed via tusk report-issue — omit line if zero)
**Lint rules**: K applied inline, M deferred as tasks
**Auto-applied**: P frontmatter edits — <one entry per item from $AUTO_APPLIED, format: `path/to/SKILL.md (brief description)`> (omit line if P == 0)
**Skipped**: M duplicates
```

Then show the current backlog:

```bash
tusk -header -column "SELECT id, summary, priority, domain, task_type, status FROM tasks WHERE status = 'To Do' ORDER BY priority_score DESC, id"
```

### LR-3a: Record approved findings for cross-retro theme detection

Before closing the skill run, write one `retro_findings` row per **approved** finding (task created, criterion added, context atom updated, issue filed, lint rule added, convention added, or skill-patched inline). Skipped/duplicate findings are **not** recorded — only actioned ones feed the cross-retro signal. For each approved finding, run:

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
- `criterion:<id>` — an acceptance criterion was added via `tusk criteria add`
- `context:<id>` — a context atom was added, resolved, or superseded via `tusk context`
- `issue:<url>` — a GitHub issue was filed via `tusk report-issue`
- `lint:<id>` — a lint rule was staged via `tusk lint-rule propose` (advisory) or added via `tusk lint-rule add`
- `convention:<id>` — a convention was added via `tusk conventions add`
- `skill-patch:<file>` — an inline edit was applied to a skill or agent doc
- `doc-patch:<file>` — an inline edit was applied to README.md or a file under docs/
- `documented` — recorded without a concrete action (e.g. noted for context)

**Omit** `--task-id` entirely when no `RETRO_TASK_ID` was captured in Step 0 — the wrapper stores a real SQL NULL. Do not pass `--task-id NULL` or `--task-id ""`. Text fields are passed as normal argparse arguments; no `$(tusk sql-quote ...)` is required. The wrapper validates `skill_run_id` (and `task_id` if supplied) as real FKs before the INSERT, so a typo'd id fails fast with exit 1.

Finally, close out the retro skill-run so its cost is captured:

```bash
tusk skill-run finish <run_id>
```

**End of lightweight retro.** Do not continue to FULL-RETRO.md.

---

## Customization

To override the default analysis categories, create a `FOCUS.md` file in the skill directory (replace `<base_directory>` with the actual path shown at the top of the loaded skill — typically `.claude/skills/retro`):

```
cp .claude/skills/retro/FOCUS.md.example .claude/skills/retro/FOCUS.md
# Edit FOCUS.md to define your custom categories
```

A template is available at `<base_directory>/FOCUS.md.example` showing the default category format. Custom categories replace A/B/C/D. Include a **"Lint Rules"** section to retain lint-rule handling and a documentation-update category if you still want retro to check docs drift.

`FOCUS.md` is not part of the distributed skill and will not be overwritten by `tusk upgrade`.
