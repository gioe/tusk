---
name: retro
description: Review the current session, surface process improvements and tangential issues, and create follow-up tasks
allowed-tools: Bash, Read
---

# Retrospective Skill

Reviews the current conversation history to capture process learnings, instruction improvements, and tangential issues. Creates structured follow-up tasks so nothing falls through the cracks.

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

Analyze the full conversation context. Look for:

- **Category A**: Process improvements — friction in skills, CLAUDE.md, tooling
- **Category B**: Tangential issues — bugs, tech debt, architectural concerns discovered out of scope
- **Category C**: Follow-up work — incomplete items, deferred decisions, edge cases
- **Category D**: Lint Rules — concrete, grep-detectable anti-patterns observed in this session (max 3). Only include if an actual mistake occurred that a grep rule could prevent — e.g., calling a deprecated command, using a wrong pattern in a specific file type. Do NOT include general advice or style preferences.

If **all categories are empty**, report "Clean session — no findings" and stop. (Config and backlog were already fetched in Step 0 — no additional work needed.)

### LR-2: Create Tasks (only if findings exist)

1. Compare each finding against the backlog for semantic overlap (use `backlog` from Step 0). Drop any already covered.

2. Run heuristic dupe check on surviving findings:
   ```bash
   tusk dupes check "<proposed summary>"
   ```

3. Present findings and proposed tasks in a table. Wait for explicit user approval before inserting.

4. Insert approved tasks:
   ```bash
   tusk task-insert "<summary>" "<description>" --priority "<priority>" --domain "<domain>" --task-type "<task_type>" --assignee "<assignee>" --complexity "<complexity>" \
     --criteria "<criterion 1>" [--criteria "<criterion 2>" ...]
   ```
   Always include at least one `--criteria` flag — derive 1–3 concrete acceptance criteria from the task description. Omit `--domain` or `--assignee` entirely if the value is NULL/empty. Exit code 1 means duplicate — skip. Skip subsumption and dependency proposals.

### LR-2b: Create Lint Rule Tasks (only if Category D has findings)

For each Category D finding, create a task whose description contains the exact `tusk lint-rule add` invocation. The retro identifies the pattern and files; the implementing agent runs the command.

The bar is high — only create a lint rule task if you observed an **actual mistake** that a grep rule would have caught. Do not create lint rule tasks for general advice.

```bash
tusk task-insert "Add lint rule: <short description>" \
  "Run: tusk lint-rule add '<pattern>' '<file_glob>' '<message>'" \
  --priority "Low" --task-type "chore" --complexity "XS" \
  --criteria "tusk lint-rule add has been run with the specified pattern, glob, and message"
```

Fill in `<pattern>` (grep regex), `<file_glob>` (e.g., `*.md` or `bin/tusk-*.py`), and `<message>` (human-readable warning) with the specific values from your finding.

### LR-3: Report

```markdown
## Retrospective Complete (Lightweight)

**Session**: <what was accomplished>
**Findings**: X total (A process / B tangential / C follow-up / D lint rules)
**Created**: N tasks (#id, #id)
**Lint rule tasks created**: K
**Skipped**: M duplicates
```

Then show the current backlog:

```bash
tusk -header -column "SELECT id, summary, priority, domain, task_type, status FROM tasks WHERE status = 'To Do' ORDER BY priority_score DESC, id"
```

**End of lightweight retro.** Do not continue to FULL-RETRO.md.
