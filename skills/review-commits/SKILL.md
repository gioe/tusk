---
name: review-commits
description: Run parallel AI code reviewers against the task's git diff, fix must_fix issues, and defer or dismiss suggestions
allowed-tools: Bash, Read, Task
---

# Review Commits Skill

Orchestrates parallel code review against the task's git diff (commits on the current branch vs the base branch). Spawns one background reviewer agent per enabled reviewer in config, monitors completion, fixes must_fix findings, handles suggest findings interactively, and creates deferred tasks for defer findings.

## Arguments

Optional: `/review-commits <task_id>` — if omitted, task ID is inferred from the current branch name.

---

## Step 1: Read Config and Check Mode

```bash
tusk config
```

Parse the returned JSON. Extract:
- `review.mode` — if `"disabled"`, print "Review mode is disabled in config (review.mode = disabled). Enable it in tusk/config.json to use /review-commits." and **stop**.
- `review.max_passes` — maximum fix-and-re-review cycles (default: 2)
- `review.reviewers` — list of reviewer names. If empty, a single unassigned review will be used.
- `review_categories` — valid comment categories (typically `["must_fix", "suggest", "defer"]`)
- `review_severities` — valid severity levels (typically `["critical", "major", "minor"]`)

## Step 2: Detect Task ID

If a task ID was passed as an argument, use it. Otherwise, infer from the current branch:

```bash
git branch --show-current
```

Parse the branch name for the pattern `TASK-<id>` (e.g., `feature/TASK-123-my-feature` → task ID 123). If no task ID can be found, ask the user to provide one.

Verify the task exists:

```bash
tusk -header -column "SELECT id, summary, status FROM tasks WHERE id = <task_id>"
```

If no row is returned, abort: "Task `<task_id>` not found."

## Step 3: Get the Git Diff

Determine the base branch and compute the diff:

```bash
git remote set-head origin --auto 2>/dev/null
DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
git diff "${DEFAULT_BRANCH}...HEAD"
```

If the diff is empty, report "No changes found compared to the base branch." and stop.

Capture the diff content — it will be passed to each reviewer agent.

## Step 4: Start Reviews

Start a review record for the task. This creates one `code_reviews` row per configured reviewer (or one unassigned row if no reviewers are configured):

```bash
tusk review start <task_id> --diff-summary "<first 120 chars of diff summary>"
```

The command prints one line per created review, each showing the review ID. Parse the output to collect all review IDs (e.g., `Started review #<id> for task #<task_id> ...`).

Store the mapping: reviewer name → review_id.

## Step 5: Spawn Parallel Reviewer Agents

Only when the diff is non-empty and reviews have been started in Step 4, read the reviewer prompt template:

```
Read file: <base_directory>/REVIEWER-PROMPT.md
```

Where `<base_directory>` is the skill base directory shown at the top of this file.

For each review_id, spawn a **background agent** using the Task tool. Issue **all Task tool calls in a single message** to run them in parallel:

```
Task tool call (for EACH review_id):
  description: "review-commits reviewer <reviewer_name or 'unassigned'> task <task_id>"
  subagent_type: general-purpose
  run_in_background: true
  prompt: <REVIEWER-PROMPT.md content, with placeholders replaced — see template>
```

Fill in these placeholders from the template:
- `{task_id}` — the task ID
- `{review_id}` — the review ID for this reviewer
- `{reviewer_name}` — the reviewer name, or "unassigned" if none
- `{diff_content}` — the full diff text
- `{review_categories}` — comma-separated list from config (e.g., `must_fix, suggest, defer`)
- `{review_severities}` — comma-separated list from config (e.g., `critical, major, minor`)

After spawning, record a map of: review_id → agent task ID.

## Step 6: Monitor Reviewer Completion

Wait for all reviewer agents to finish:

**Monitoring loop:**

1. Wait 30 seconds:
   ```bash
   sleep 30
   ```

2. Check which reviews are still pending:
   ```bash
   tusk review status <task_id>
   ```
   Parse the JSON. Reviews with `status = "pending"` are still in progress. If all reviews have `status` of `"approved"` or `"changes_requested"`, exit the loop.

3. For each pending review, check whether its agent has finished using `TaskOutput` with `block: false` and the agent task ID:
   - If **any agent is still running**, go back to step 1.
   - If **all agents have completed** but some reviews are still `"pending"`, those agents finished without calling `tusk review approve` or `tusk review request-changes`. Log a warning for each stuck review and continue as if those reviews returned no findings (treat as approved).

## Step 7: Process Findings

After all reviewer agents complete, fetch the full review results for each review:

```bash
tusk review list <task_id>
```

Gather all open (unresolved) comments across all reviews. Group them by category:

### must_fix comments

These are blocking issues that must be resolved before the work can be merged.

For each open `must_fix` comment:
1. Read the comment details (file path, line numbers, comment text, severity).
2. Implement the fix directly in the codebase.
3. After fixing, mark the comment resolved:
   ```bash
   tusk review resolve <comment_id> fixed
   ```

If there are many `must_fix` comments (more than 5), consider spawning a background implementation agent instead:

```
Task tool call:
  description: "fix must_fix review comments for task <task_id>"
  subagent_type: general-purpose
  run_in_background: false
  prompt: |
    Fix the following must_fix code review comments for task <task_id>.
    After fixing each item, mark it resolved: tusk review resolve <comment_id> fixed

    Findings to fix:
    <list each comment with file, line, and description>

    Work through them in order. Do not make unrelated changes.
```

### suggest comments

These are optional improvements. Present each one to the user:

> **Suggestion #<id>** (`<file>:<line>`): <comment text>
> Fix or dismiss? (f/d)

- **Fix (f)**: implement the suggestion and run `tusk review resolve <comment_id> fixed`
- **Dismiss (d)**: run `tusk review resolve <comment_id> dismissed`

If there are no interactive users (running in a background agent context), **dismiss all suggest comments** automatically and log them as dismissed.

### defer comments

These are valid issues but out of scope for the current work. Create a tusk task for each:

```bash
tusk task-insert "<summary from comment>" "<full comment text>" \
  --priority Medium \
  --domain <same domain as current task> \
  --task-type refactor \
  --deferred
```

After creating each deferred task, mark the comment resolved:

```bash
tusk review resolve <comment_id> deferred
```

## Step 8: Re-review Loop (if there were must_fix changes)

If any `must_fix` comments were fixed in Step 7, re-run the review to verify the fixes are correct. Cap the number of passes at `max_passes` from config.

Track current pass number (starts at 1). If `current_pass < max_passes`:

1. Get the updated diff:
   ```bash
   git diff "${DEFAULT_BRANCH}...HEAD"
   ```

2. Start a new review pass:
   ```bash
   tusk review start <task_id> --pass-num <current_pass + 1> --diff-summary "Re-review pass <n>"
   ```

3. Spawn reviewer agents again (Step 5) with the new review IDs and updated diff.

4. Monitor completion (Step 6) and process findings (Step 7).

5. Increment pass counter. If `current_pass >= max_passes` and there are still open `must_fix` items, **escalate to the user**:
   > Max review passes (<max_passes>) reached. The following must_fix items remain unresolved:
   > <list each open must_fix comment>
   >
   > Please resolve these manually before continuing.

   Stop the re-review loop.

If all `must_fix` items are resolved and no new blocking findings were raised, proceed to Step 9.

## Step 9: Final Summary

For each review ID, print the summary:

```bash
tusk review summary <review_id>
```

Then print an overall summary:

```
Review complete for Task <task_id>: <task_summary>
══════════════════════════════════════════════════
Reviewers: <count>
Pass:      <final_pass_number>

must_fix:  <total_count> found, <fixed_count> fixed
suggest:   <total_count> found, <fixed_count> fixed, <dismissed_count> dismissed
defer:     <total_count> found, <deferred_count> tasks created

Verdict: APPROVED / CHANGES REMAINING
```

The verdict is **APPROVED** if all must_fix comments are resolved (fixed). Otherwise, **CHANGES REMAINING**.
