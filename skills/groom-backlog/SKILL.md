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

## Step 0: Auto-Close Expired Deferred Tasks

Before analyzing the backlog, close any deferred tasks that have passed their 60-day expiry. This is automatic and does not require user approval.

```bash
# Collect IDs of expired deferred tasks before closing them
EXPIRED_IDS=$(tusk "
SELECT id FROM tasks
WHERE summary LIKE '%[Deferred]%'
  AND status = 'To Do'
  AND expires_at IS NOT NULL
  AND expires_at < datetime('now')
")

echo "Expired deferred tasks: $(echo "$EXPIRED_IDS" | grep -c . || echo 0)"

# Auto-close expired deferred tasks
tusk "
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

# Close any open sessions for each expired task via the CLI
for TASK_ID in $EXPIRED_IDS; do
  tusk session-close --task-id "$TASK_ID" --skip-stats
done
```

Report how many were auto-closed before proceeding.

## Step 0b: Flag In Progress Tasks with Merged PRs

Detect tasks stuck in "In Progress" whose GitHub PR has already been merged. These are orphaned tasks from sessions that ended before finalization.

```bash
# Find In Progress tasks that have a github_pr URL
tusk -header -column "
SELECT id, summary, github_pr
FROM tasks
WHERE status = 'In Progress'
  AND github_pr IS NOT NULL
  AND github_pr <> ''
"
```

For each task returned, check whether the PR has been merged:

```bash
gh pr view <pr_url> --json state,mergedAt --jq '{state: .state, mergedAt: .mergedAt}'
```

If the PR state is `MERGED`, auto-close the task and any orphaned sessions:

```bash
tusk "UPDATE tasks SET status = 'Done', closed_reason = 'completed', updated_at = datetime('now'),
  description = description || char(10) || char(10) || '---' || char(10) || 'Auto-closed: PR was already merged (' || datetime('now') || ').'
WHERE id = <id>"

# Close any open sessions for this task
tusk session-close --task-id <id> --skip-stats
```

If the PR state is `CLOSED` (not merged), flag it for user review in Step 3 with a note that the PR was closed without merging.

Report how many orphaned In Progress tasks were auto-closed before proceeding.

## Step 0c: Cascade-Close Moot Contingent Tasks

Find open tasks that have a `contingent` dependency on a task that closed as `wont_do` or `expired`. These tasks are moot — the upstream evaluation/investigation determined the work isn't needed.

```bash
# Find contingent dependents of tasks closed as wont_do/expired
tusk -header -column "
SELECT t.id, t.summary, t.status,
       d.depends_on_id as closed_task_id,
       upstream.summary as closed_task_summary,
       upstream.closed_reason
FROM tasks t
JOIN task_dependencies d ON t.id = d.task_id
JOIN tasks upstream ON d.depends_on_id = upstream.id
WHERE t.status <> 'Done'
  AND d.relationship_type = 'contingent'
  AND upstream.status = 'Done'
  AND upstream.closed_reason IN ('wont_do', 'expired')
"
```

For each match, auto-close as `wont_do`:

```bash
tusk "UPDATE tasks SET status = 'Done', closed_reason = 'wont_do',
  updated_at = datetime('now'),
  description = description || char(10) || char(10) || '---' || char(10) ||
    'Auto-closed: Contingent on TASK-<upstream_id> which closed as <reason> (' || datetime('now') || ').'
WHERE id = <id>"
```

Report how many contingent tasks were auto-closed before proceeding.

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

After all grooming changes and sizing are complete, read the reference file for the WSJF scoring formula and UPDATE SQL:

```
Read file: <base_directory>/REFERENCE.md
```

Run the WSJF update from the Step 7 section of the reference, then generate the summary report:

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
