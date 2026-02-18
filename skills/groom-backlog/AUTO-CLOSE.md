# Auto-Close Steps

These steps run conditionally based on the pre-check counts in SKILL.md. Execute **only** the steps whose count is non-zero.

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
