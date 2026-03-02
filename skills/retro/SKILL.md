---
name: retro
description: Review the current session, surface process improvements and tangential issues, and create follow-up tasks
allowed-tools: Bash, Read, Edit
---

# Retrospective Skill

Reviews the current conversation history to capture process learnings, instruction improvements, and tangential issues. Creates structured follow-up tasks so nothing falls through the cracks.

> **Prefer `/create-task` for all task creation.** It handles decomposition, deduplication, acceptance criteria generation, and dependency proposals in one workflow. Use `bin/tusk task-insert` directly only when scripting bulk inserts or in automated contexts where the interactive review step is not applicable.

## Step 0: Setup

Fetch config, backlog, then determine retro mode:

```bash
tusk "SELECT complexity FROM tasks WHERE status = 'Done' ORDER BY updated_at DESC LIMIT 1"
tusk setup
```

Parse the JSON from `tusk setup`: use `config` for metadata assignment and `backlog` for duplicate comparison.

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

**Check for custom focus areas first.** Attempt to read `<base_directory>/FOCUS.md`.
- If the file exists: use the categories defined in it for the analysis below.
- If the file does not exist: use the default categories:
  - **Category A**: Process improvements — friction in skills, CLAUDE.md, tooling
  - **Category B**: Tangential issues — bugs, tech debt, architectural concerns discovered out of scope
  - **Category C**: Follow-up work — incomplete items, deferred decisions, edge cases
  - **Category D**: Lint Rules — concrete, grep-detectable anti-patterns observed in this session (max 3). Only include if an actual mistake occurred that a grep rule could prevent — e.g., calling a deprecated command, using a wrong pattern in a specific file type. Do NOT include general advice or style preferences.
  - **Category E**: Debugging Velocity — only if the session involved fixing a bug or diagnosing unexpected behavior. Reflect on: what information was missing that delayed diagnosis; what tool, log, or trace would have surfaced the root cause immediately; whether a test would have caught this before it became a bug. If no bug was present, this category is empty. Findings must be concrete (tasks or skill/CLAUDE.md patches) — not generic advice like "add more logging."

Analyze the full conversation context using the resolved categories.

If **all categories are empty**, report "Clean session — no findings" and stop. (Config and backlog were already fetched in Step 0 — no additional work needed.)

### LR-2: Create Tasks (only if findings exist)

1. Compare each finding against the backlog for semantic overlap (use `backlog` from Step 0). Drop any already covered.

2. Run heuristic dupe check on surviving findings:
   ```bash
   tusk dupes check "<proposed summary>"
   ```

3. Present findings and proposed tasks in a table. Wait for explicit user approval before inserting.

4. For **Category A and Category E** approved findings, follow **LR-2a** below before inserting tasks. For all other approved findings, insert tasks now:
   ```bash
   tusk task-insert "<summary>" "<description>" --priority "<priority>" --domain "<domain>" --task-type "<task_type>" --assignee "<assignee>" --complexity "<complexity>" \
     --criteria "<criterion 1>" [--criteria "<criterion 2>" ...]
   ```
   Always include at least one `--criteria` flag — derive 1–3 concrete acceptance criteria from the task description. Omit `--domain` or `--assignee` entirely if the value is NULL/empty. Exit code 1 means duplicate — skip. Skip subsumption and dependency proposals.

### LR-2a: Skill-Patch for Category A and Category E Findings (only if Category A or Category E findings exist)

Before creating tasks for Category A (process improvement) or Category E (debugging velocity) findings, check if any can be applied as inline patches to an existing skill or CLAUDE.md.

For each approved Category A finding:

1. **Identify a target file** — check whether the finding description mentions:
   - A skill name matching a directory in `.claude/skills/` (list them with `ls .claude/skills/`)
   - The string `CLAUDE.md`

2. **If a target file is identified**:
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
      > **defer** — create a task with this diff included in the description
      > **skip** — create a generic task as usual

3. **If approved**: apply the edit in-session using the Edit tool. Do **not** create a task for this finding.

4. **If deferred**: include the proposed diff verbatim in the task description when calling `tusk task-insert`.

5. **If skipped, or if no target file was identified**: proceed to normal task creation (step 4 above).

### LR-2b: Apply Lint Rules Inline (only if lint rule findings exist)

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

### LR-3: Report

```markdown
## Retrospective Complete (Lightweight)

**Session**: <what was accomplished>
**Findings**: X total (by category — use resolved category names)
**Created**: N tasks (#id, #id)
**Lint rules**: K applied inline, M deferred as tasks
**Skipped**: M duplicates
```

Then show the current backlog:

```bash
tusk -header -column "SELECT id, summary, priority, domain, task_type, status FROM tasks WHERE status = 'To Do' ORDER BY priority_score DESC, id"
```

**End of lightweight retro.** Do not continue to FULL-RETRO.md.

---

## Customization

To override the default analysis categories, create a `FOCUS.md` file in the skill directory (replace `<base_directory>` with the actual path shown at the top of the loaded skill — typically `.claude/skills/retro`):

```
cp .claude/skills/retro/FOCUS.md.example .claude/skills/retro/FOCUS.md
# Edit FOCUS.md to define your custom categories
```

A template is available at `<base_directory>/FOCUS.md.example` showing the default category format. Custom categories replace A–D. Include a **"Lint Rules"** section to retain lint-rule handling.

`FOCUS.md` is not part of the distributed skill and will not be overwritten by `tusk upgrade`.
