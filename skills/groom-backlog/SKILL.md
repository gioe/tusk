---
name: groom-backlog
description: Groom the backlog by closing completed tickets, removing redundant/stale tickets, reprioritizing, and assigning agents
allowed-tools: Bash, Glob, Grep, Read
---

# Groom Backlog Skill

Grooms the local task database by identifying completed, redundant, incorrectly prioritized, or unassigned tasks.

## Setup: Discover Project Config

Before grooming, discover valid values for this project:

```bash
tusk config
```

This returns the full config as JSON (domains, agents, task_types, priorities, complexity, etc.). Use these values (not hardcoded ones) throughout the grooming process.

## Setup: Read Project Conventions

Fetch learned project heuristics so they inform grooming decisions:

```bash
tusk conventions
```

If the file is missing or contains only the header comment (no convention entries), skip this step silently. Otherwise, hold the conventions in context as **preamble rules** for the analysis in Steps 1–2. Conventions influence how you evaluate tasks — for example, a convention about file coupling patterns may reveal that two apparently separate tasks are really one piece of work (candidates for merging), or that a task is missing implicit sub-work.

## Pre-Check: Count Auto-Close Candidates

Run a single combined query to determine which auto-close steps (0, 0b, 0c) have work to do. Steps with a zero count are skipped entirely.

```bash
tusk -header -column "
SELECT
  (SELECT COUNT(*) FROM tasks
   WHERE summary LIKE '%[Deferred]%'
     AND status = 'To Do'
     AND expires_at IS NOT NULL
     AND expires_at < datetime('now')
  ) AS expired_deferred,

  (SELECT COUNT(*) FROM tasks
   WHERE status = 'In Progress'
     AND github_pr IS NOT NULL
     AND github_pr <> ''
  ) AS in_progress_with_pr,

  (SELECT COUNT(*) FROM tasks t
   JOIN task_dependencies d ON t.id = d.task_id
   JOIN tasks upstream ON d.depends_on_id = upstream.id
   WHERE t.status <> 'Done'
     AND d.relationship_type = 'contingent'
     AND upstream.status = 'Done'
     AND upstream.closed_reason IN ('wont_do', 'expired')
  ) AS moot_contingent
"
```

Interpret the results:
- If **all three counts are zero**: report "No auto-close candidates found — skipping Steps 0/0b/0c" and jump directly to **Step 1**.
- If any count is non-zero, read the companion file for auto-close steps:

  ```
  Read file: <base_directory>/AUTO-CLOSE.md
  ```

  Where `<base_directory>` is the skill base directory shown at the top of this file. Execute **only** the steps whose count is non-zero:
  - `expired_deferred > 0` → run **Step 0**
  - `in_progress_with_pr > 0` → run **Step 0b**
  - `moot_contingent > 0` → run **Step 0c**

## Step 1: Fetch All Backlog Tasks

```bash
tusk -header -column "
SELECT id, summary, status, priority, domain, assignee, complexity, task_type, priority_score
FROM tasks
WHERE status <> 'Done'
ORDER BY priority_score DESC, id
"

tusk deps blocked
tusk deps all
```

**On-demand descriptions**: This query intentionally omits the `description` column to keep context lean. When you identify action candidates in Step 2 (tasks to close, delete, reprioritize, or assign), fetch full details for just those tasks:

```bash
tusk -header -column "SELECT id, summary, description FROM tasks WHERE id IN (<comma-separated ids>)"
```

## Step 2: Scan for Duplicates and Categorize Tasks

### Step 2a: Scan for Duplicate Pairs

```bash
tusk dupes scan --status "To Do"
```

Any pairs found should be included in **Category B** with reason "duplicate".

### Step 2b: Categorize Tasks

Analyze each task and categorize. In addition to the heuristic scan results from Step 2a, look for **semantic duplicates** — tasks that cover the same intent but use different wording (e.g., "Implement password reset flow" vs. "Add forgot password endpoint"). The heuristic catches textual near-matches; you should catch conceptual overlap that differs in phrasing.

### Category A: Candidates for Done (Acceptance Criteria Already Met)
Tasks where the work has already been completed in the codebase:
1. **Verify against code**: Search the codebase to determine if the work is done
2. **Evidence required**: Provide specific file paths and code as proof
3. **Mark as Done**:
   ```bash
   tusk "UPDATE tasks SET status = 'Done', closed_reason = 'completed', updated_at = datetime('now') WHERE id = <id>"
   ```

### Category B: Candidates for Deletion
- **Redundant tasks**: Duplicates or near-duplicates
- **Obsolete tasks**: No longer relevant
- **Stale tasks**: Untouched with no clear path forward
- **Vague tasks**: Insufficient detail to act on

Before recommending deletion, check dependents:
```bash
tusk deps dependents <id>
```

### Category C: Candidates for Reprioritization
- **Under-prioritized**: Security issues, user-facing bugs
- **Over-prioritized**: Nice-to-haves, speculative work

### Category D: Unassigned Tasks
Tasks without an agent assignee:

```bash
tusk -header -column "SELECT id, summary, domain FROM tasks WHERE status <> 'Done' AND assignee IS NULL"
```

Assign based on project agents (from `tusk config`).

### Category E: Healthy Tasks
Correctly prioritized, assigned, and relevant. No action needed.

## Step 3: Present Findings for Approval

Present analysis in this format:

```markdown
## Backlog Grooming Analysis

### Total Tasks Analyzed: X

### Ready for Done (W tasks)
| ID | Summary | Evidence |

### Recommended for Deletion (Y tasks)
| ID | Summary | Reason |

### Recommended for Reprioritization (Z tasks)
| ID | Summary | Current | Recommended | Reason |

### Unassigned Tasks (U tasks)
| ID | Summary | Recommended Agent | Reason |

### No Action Needed (V tasks)
```

## Step 4: Get User Confirmation

**IMPORTANT**: Before making any changes, explicitly ask the user to approve each category.

## Step 5: Execute Changes

Only after user approval:

### For Done Transitions:
```bash
tusk "UPDATE tasks SET status = 'Done', closed_reason = 'completed', updated_at = datetime('now') WHERE id = <id>"
```

### For Deletions:
```bash
# Duplicates:
tusk "UPDATE tasks SET status = 'Done', closed_reason = 'duplicate', updated_at = datetime('now') WHERE id = <id>"

# Obsolete/won't-do:
tusk "UPDATE tasks SET status = 'Done', closed_reason = 'wont_do', updated_at = datetime('now') WHERE id = <id>"
```

### For Priority Changes:
```bash
tusk "UPDATE tasks SET priority = '<New Priority>' WHERE id = <id>"
```

### For Agent Assignments:
```bash
tusk "UPDATE tasks SET assignee = '<agent-name>' WHERE id = <id>"
```

### After All Changes:

Verify all modifications in a single batch query:

```bash
tusk -header -column "SELECT id, summary, status, priority, assignee FROM tasks WHERE id IN (<comma-separated ids of all changed tasks>)"
```

## Step 6: Bulk-Estimate Unsized Tasks

Before computing priority scores, check for tasks without complexity estimates:

```bash
tusk -header -column "
SELECT id, summary, description, domain, task_type
FROM tasks
WHERE status <> 'Done'
  AND complexity IS NULL
ORDER BY id
"
```

If no rows are returned, skip to Step 7.

If unsized tasks are found, read the reference file for the sizing workflow:

```
Read file: <base_directory>/REFERENCE.md
```

Follow Steps 6b–6d from the reference, then continue to Step 7 below.

## Step 7: Compute Priority Scores and Final Report

After all grooming changes and sizing are complete, run the WSJF scoring command:

```bash
tusk wsjf
```

Then generate the summary report:

```markdown
## Backlog Grooming Complete

### Actions Taken:
- **Moved to Done**: W tasks
- **Deleted**: X tasks
- **Reprioritized**: Y tasks
- **Assigned**: U tasks
- **Unchanged**: Z tasks
```

Show the final backlog state (this also serves as WSJF score verification):

```bash
tusk -header -column "
SELECT id, summary, status, priority, complexity, priority_score, domain, assignee
FROM tasks
WHERE status <> 'Done'
ORDER BY priority_score DESC, id
"
```

## Important Guidelines

1. **Never modify without approval**: Always present findings and wait for explicit user confirmation
2. **Verify against code thoroughly**: Use Glob, Grep, and Read to find evidence
3. **Be conservative**: When in doubt, keep the task open
4. **Preserve history**: Close with a reason rather than DELETE
5. **Consider dependencies**: Check dependents before deleting
6. **Batch operations carefully**: Execute changes one at a time
7. **Keep the backlog lean (< 20 open tasks)**: The full backlog dump scales at ~700 tokens/task and is repeated across ~15+ agentic turns during grooming. A 30-task backlog can consume over 300k tokens in a single session. Aggressively close, merge, or defer tasks to stay under 20 open items
