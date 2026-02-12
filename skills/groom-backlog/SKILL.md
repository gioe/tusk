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
.claude/bin/tusk config domains
.claude/bin/tusk config agents
.claude/bin/tusk config task_types
```

Use these values (not hardcoded ones) throughout the grooming process.

## Step 0: Auto-Close Expired Deferred Tasks

Before analyzing the backlog, close any deferred tasks that have passed their 60-day expiry. This is automatic and does not require user approval.

```bash
# Check how many deferred tasks have expired
.claude/bin/tusk -header -column "
SELECT COUNT(*) as expired_count
FROM tasks
WHERE summary LIKE '%[Deferred]%'
  AND status = 'To Do'
  AND expires_at IS NOT NULL
  AND expires_at < datetime('now')
"

# Auto-close expired deferred tasks
.claude/bin/tusk "
UPDATE tasks
SET status = 'Done',
    closed_reason = 'expired',
    updated_at = datetime('now'),
    description = description || char(10) || char(10) || '---' || char(10) || 'Auto-closed: Deferred task expired after 60 days without action (' || datetime('now') || ').'
WHERE summary LIKE '%[Deferred]%'
  AND status = 'To Do'
  AND expires_at IS NOT NULL
  AND expires_at < datetime('now');
"
```

Report how many were auto-closed before proceeding.

## Step 1: Fetch All Backlog Tasks

```bash
.claude/bin/tusk -header -column "SELECT id, summary, status, priority, domain, assignee FROM tasks WHERE status != 'Done' ORDER BY priority DESC, id"

.claude/bin/tusk -header -column "SELECT * FROM tasks WHERE status != 'Done'"

python3 scripts/manage_dependencies.py blocked
python3 scripts/manage_dependencies.py all
```

## Step 2: Scan for Duplicates and Categorize Tasks

### Step 2a: Scan for Duplicate Pairs

```bash
python3 scripts/check_duplicates.py scan --status "To Do"
```

Any pairs found should be included in **Category B** with reason "duplicate".

### Step 2b: Categorize Tasks

Analyze each task and categorize:

### Category A: Candidates for Done (Acceptance Criteria Already Met)
Tasks where the work has already been completed in the codebase:
1. **Verify against code**: Search the codebase to determine if the work is done
2. **Evidence required**: Provide specific file paths and code as proof
3. **Mark as Done**:
   ```bash
   .claude/bin/tusk "UPDATE tasks SET status = 'Done', closed_reason = 'completed', updated_at = datetime('now') WHERE id = <id>"
   ```

### Category B: Candidates for Deletion
- **Redundant tasks**: Duplicates or near-duplicates
- **Obsolete tasks**: No longer relevant
- **Stale tasks**: Untouched with no clear path forward
- **Vague tasks**: Insufficient detail to act on

Before recommending deletion, check dependents:
```bash
python3 scripts/manage_dependencies.py dependents <id>
```

### Category C: Candidates for Reprioritization
- **Under-prioritized**: Security issues, user-facing bugs
- **Over-prioritized**: Nice-to-haves, speculative work

### Category D: Unassigned Tasks
Tasks without an agent assignee:

```bash
.claude/bin/tusk -header -column "SELECT id, summary, domain FROM tasks WHERE status != 'Done' AND assignee IS NULL"
```

Assign based on project agents (from `tusk config agents`).

### Category E: Healthy Tasks
Correctly prioritized, assigned, and relevant. No action needed.

## Step 3: Review Context

```bash
.claude/bin/tusk -header -column "SELECT * FROM tasks WHERE id = <id>"
```

Also review recent commits and project documentation to understand current priorities.

## Step 4: Present Findings for Approval

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

## Step 5: Get User Confirmation

**IMPORTANT**: Before making any changes, explicitly ask the user to approve each category.

## Step 6: Execute Changes

Only after user approval:

### For Done Transitions:
```bash
.claude/bin/tusk "UPDATE tasks SET status = 'Done', closed_reason = 'completed', updated_at = datetime('now') WHERE id = <id>"
```

### For Deletions:
```bash
# Duplicates:
.claude/bin/tusk "UPDATE tasks SET status = 'Done', closed_reason = 'duplicate', updated_at = datetime('now') WHERE id = <id>"

# Obsolete/won't-do:
.claude/bin/tusk "UPDATE tasks SET status = 'Done', closed_reason = 'wont_do', updated_at = datetime('now') WHERE id = <id>"
```

### For Priority Changes:
```bash
.claude/bin/tusk "UPDATE tasks SET priority = '<New Priority>' WHERE id = <id>"
```

### For Agent Assignments:
```bash
.claude/bin/tusk "UPDATE tasks SET assignee = '<agent-name>' WHERE id = <id>"
```

### After Each Change:
```bash
.claude/bin/tusk -header -column "SELECT id, summary, status, priority FROM tasks WHERE id = <id>"
```

## Step 7: Compute Priority Scores

After all grooming changes are complete, compute `priority_score` for all open tasks.

### Scoring Formula

```
priority_score = base_priority + source_bonus + unblocks_bonus
```

Where:
- **base_priority**: Highest=100, High=80, Medium=60, Low=40, Lowest=20
- **source_bonus**: +10 if NOT a `[Deferred]` task
- **unblocks_bonus**: +5 per dependent task, capped at +15

```bash
.claude/bin/tusk "
UPDATE tasks SET priority_score = (
  CASE priority
    WHEN 'Highest' THEN 100
    WHEN 'High' THEN 80
    WHEN 'Medium' THEN 60
    WHEN 'Low' THEN 40
    WHEN 'Lowest' THEN 20
    ELSE 40
  END
  + CASE WHEN summary NOT LIKE '%[Deferred]%' THEN 10 ELSE 0 END
  + MIN(COALESCE((
    SELECT COUNT(*) * 5
    FROM task_dependencies d
    WHERE d.depends_on_id = tasks.id
  ), 0), 15)
)
WHERE status != 'Done';
"

# Verify
.claude/bin/tusk -header -column "
SELECT id, summary, priority, priority_score
FROM tasks
WHERE status = 'To Do'
ORDER BY priority_score DESC
LIMIT 15;
"
```

## Step 8: Generate Summary Report

```markdown
## Backlog Grooming Complete

### Actions Taken:
- **Moved to Done**: W tasks
- **Deleted**: X tasks
- **Reprioritized**: Y tasks
- **Assigned**: U tasks
- **Unchanged**: Z tasks
```

Show final backlog state:
```bash
.claude/bin/tusk -header -column "SELECT id, summary, status, priority, domain, assignee FROM tasks WHERE status != 'Done' ORDER BY priority DESC, id"
```

## Important Guidelines

1. **Never modify without approval**: Always present findings and wait for explicit user confirmation
2. **Verify against code thoroughly**: Use Glob, Grep, and Read to find evidence
3. **Be conservative**: When in doubt, keep the task open
4. **Preserve history**: Close with a reason rather than DELETE
5. **Consider dependencies**: Check dependents before deleting
6. **Batch operations carefully**: Execute changes one at a time
