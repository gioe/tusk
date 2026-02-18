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

If **all categories are empty**, report "Clean session — no findings" and stop. Do not fetch config or backlog.

### LR-2: Create Tasks (only if findings exist)

1. Fetch config and open backlog:
   ```bash
   tusk config
   tusk -header -column "SELECT id, summary, domain, priority FROM tasks WHERE status <> 'Done' ORDER BY id"
   ```

2. Compare each finding against the backlog for semantic overlap. Drop any already covered.

3. Run heuristic dupe check on surviving findings:
   ```bash
   tusk dupes check "<proposed summary>"
   ```

4. Present findings and proposed tasks in a table. Wait for explicit user approval before inserting.

5. Insert approved tasks:
   ```bash
   tusk "INSERT INTO tasks (summary, description, status, priority, domain, task_type, assignee, complexity, created_at, updated_at)
     VALUES ($(tusk sql-quote "<summary>"), $(tusk sql-quote "<description>"), 'To Do', '<priority>', '<domain_or_NULL>', '<task_type>', '<assignee_or_NULL>', '<complexity_or_NULL>', datetime('now'), datetime('now'))"
   ```
   Use unquoted `NULL` for empty fields. Skip subsumption and dependency proposals.

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

**End of lightweight retro.** Do not continue to FULL-RETRO.md.
