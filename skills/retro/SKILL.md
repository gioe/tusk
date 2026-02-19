---
name: retro
description: Review the current session, surface process improvements and tangential issues, and create follow-up tasks
allowed-tools: Bash, Read
---

# Retrospective Skill

Reviews the current conversation history to capture process learnings, instruction improvements, and tangential issues. Creates structured follow-up tasks so nothing falls through the cracks.

## Step 0: Determine Retro Mode

Check the complexity of the task that was just completed:

```bash
tusk "SELECT complexity FROM tasks WHERE status = 'Done' ORDER BY updated_at DESC LIMIT 1"
```

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
- **Category D**: Conventions — generalizable project heuristics worth codifying (file coupling patterns, decomposition rules, naming conventions, workflow patterns that recur across sessions)

Category D examples: "bin/tusk-*.py always needs a dispatcher entry in bin/tusk", "schema migrations require bumping both user_version and tusk init", "skills that INSERT tasks must run dupe check first".

If **all categories are empty**, report "Clean session — no findings" and stop. Do not fetch config or backlog.

### LR-2: Create Tasks (only if findings exist)

1. Fetch config, backlog, and conventions in one call:
   ```bash
   tusk setup
   ```
   Parse the JSON: use `config` for metadata assignment, `backlog` for duplicate comparison, and `conventions` for LR-2b.

2. Compare each finding against the backlog for semantic overlap. Drop any already covered.

3. Run heuristic dupe check on surviving findings:
   ```bash
   tusk dupes check "<proposed summary>"
   ```

4. Present findings and proposed tasks in a table. Wait for explicit user approval before inserting.

5. Insert approved tasks:
   ```bash
   tusk task-insert "<summary>" "<description>" --priority "<priority>" --domain "<domain>" --task-type "<task_type>" --assignee "<assignee>" --complexity "<complexity>"
   ```
   Omit `--domain` or `--assignee` entirely if the value is NULL/empty. Exit code 1 means duplicate — skip. Skip subsumption and dependency proposals.

### LR-2b: Write Conventions (only if Category D has findings)

For each Category D finding, check whether it is already captured in the `conventions` string from `tusk setup` (fetched in LR-2 step 1).

Skip any convention whose meaning is already present (even if worded differently). For each new convention, append it:

```bash
tusk "SELECT id FROM task_sessions WHERE task_id = (SELECT id FROM tasks WHERE status = 'Done' ORDER BY updated_at DESC LIMIT 1) ORDER BY id DESC LIMIT 1"
```

Use the session ID and current date to stamp the entry. Append to `tusk/conventions.md` using this format (one block per convention):

```markdown

## <short title>
_Source: session <session_id> — <YYYY-MM-DD>_

<one-to-two sentence description of the convention and when it applies>
```

Do not reorder or delete existing entries — always append at the end of the file.

### LR-3: Report

```markdown
## Retrospective Complete (Lightweight)

**Session**: <what was accomplished>
**Findings**: X total (A process / B tangential / C follow-up / D conventions)
**Created**: N tasks (#id, #id)
**Conventions written**: K new (L skipped as duplicates)
**Skipped**: M duplicates
```

Then show the current backlog:

```bash
tusk -header -column "SELECT id, summary, priority, domain, task_type, status FROM tasks WHERE status = 'To Do' ORDER BY priority_score DESC, id"
```

**End of lightweight retro.** Do not continue to FULL-RETRO.md.
