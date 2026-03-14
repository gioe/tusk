---
name: review-commits
description: Run parallel AI code reviewers against the task's git diff, fix must_fix issues, and defer or dismiss suggestions
allowed-tools: Bash, Read, Task
---

# Review Commits Skill

Orchestrates parallel code review against the task's git diff (commits on the current branch vs the base branch). Spawns one background reviewer agent per enabled reviewer in config, monitors completion, fixes must_fix findings, handles suggest findings interactively, and creates deferred tasks for defer findings.

> **Prefer `/create-task` for all task creation.** It handles decomposition, deduplication, acceptance criteria generation, and dependency proposals in one workflow. Use `bin/tusk task-insert` directly only when scripting bulk inserts or in automated contexts where the interactive review step is not applicable.

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
- `review.reviewers` — list of reviewer objects (each with `name` and `description` fields). If empty, a single unassigned review will be used.
- `review_categories` — valid comment categories (typically `["must_fix", "suggest", "defer"]`)
- `review_severities` — valid severity levels (typically `["critical", "major", "minor"]`)

## Step 2: Detect Task ID

If a task ID was passed as an argument, use it. Otherwise, infer from the current branch:

```bash
git branch --show-current
```

Parse the branch name for the pattern `TASK-<id>` (e.g., `feature/TASK-123-my-feature` → task ID 123). If no task ID can be found, ask the user to provide one.

Verify the task exists and capture its domain:

```bash
tusk -header -column "SELECT id, summary, status, domain FROM tasks WHERE id = <task_id>"
```

If no row is returned, abort: "Task `<task_id>` not found."

Store the task's `domain` value (may be NULL/empty — this is used to filter reviewers in Step 5).

## Step 3: Get the Git Diff

Determine the base branch and compute the diff:

```bash
git remote set-head origin --auto 2>/dev/null
DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
CURRENT_BRANCH=$(git branch --show-current)
git diff "${DEFAULT_BRANCH}...HEAD"
```

If the diff is empty **and** `CURRENT_BRANCH == DEFAULT_BRANCH` (i.e., working directly on the default branch), fall back to the last commit:

```bash
git diff HEAD~1..HEAD
```

If the diff is still empty after the fallback (or if on a feature branch with no changes), report "No changes found compared to the base branch." and stop.

Capture the diff only to check for emptiness and to generate the `--diff-summary` for `tusk review start`. **Do not pass the diff to reviewer agents** — they will fetch it themselves via `git diff` to avoid transcription errors.

## Step 4: Start Reviews

Start a review record for the task. This creates one `code_reviews` row per configured reviewer (or one unassigned row if no reviewers are configured):

```bash
tusk review start <task_id> --diff-summary "<first 120 chars of diff summary>"
```

The command prints one line per created review, each showing the review ID. Parse the output to collect all review IDs (e.g., `Started review #<id> for task #<task_id> ...`).

Store the mapping: reviewer name → review_id.

## Step 5: Spawn Parallel Reviewer Agents

Only when the diff is non-empty and reviews have been started in Step 4, proceed with the steps below.

### Step 5.1: Choose review strategy and verify permissions

> **Important:** Background reviewer agents run in an **isolated sandbox** and do **not** inherit the parent session's tool permissions. Approving Bash in this conversation does not grant Bash access to spawned agents. The `permissions.allow` block in `.claude/settings.json` is the only reliable way to grant tool access in agent sandboxes — it applies to all subagents spawned from this project, regardless of what is auto-approved in the current session.

**For small or documentation-only diffs (fewer than ~200 lines changed, or only non-code files such as `.md`, `.json`, `.yaml`):** skip agent spawning and perform an inline review instead. Read the diff yourself, evaluate it against the reviewer focus areas, and record the result directly:

```bash
# Approve with no findings:
tusk review approve <review_id> --note "Inline review: small/docs-only diff, no findings."
# Or if changes are needed:
tusk review request-changes <review_id>
# Then add comments as needed:
tusk review comment add <review_id> <category> <severity> "<file>:<line>" "<description>"
```

After recording the inline decision, skip directly to Step 6.

**For all other diffs:** verify Bash is accessible before spawning reviewer agents. Run:

```bash
tusk version
```

If this command fails with a permissions error (Bash not permitted), stop and surface:
> Agent review aborted: Bash is not accessible in agent sandboxes for this project. The `permissions.allow` block in `.claude/settings.json` must include the required entries: `Bash(git diff:*)`, `Bash(git remote:*)`, `Bash(git symbolic-ref:*)`, `Bash(git branch:*)`, `Bash(tusk review:*)`. Run `tusk upgrade` to apply them, then restart the session.

Proceed to spawn agents only if `tusk version` succeeds.

Read the reviewer prompt template:

```
Read file: <base_directory>/REVIEWER-PROMPT.md
```

Where `<base_directory>` is the skill base directory shown at the top of this file.

**Filter reviewers by task domain before spawning:**

Using the `reviewer name → review_id` mapping from Step 4 and the task domain from Step 2, determine which reviewers to spawn:

- A reviewer with an **empty or absent `domains` array** → always spawn (general reviewer).
- A reviewer with a **non-empty `domains` array** → spawn only if the task's domain appears in that array.
- If the task has **no domain (NULL/empty)** → spawn only reviewers with an empty or absent `domains` array.

For each review_id whose reviewer was filtered out by this logic, immediately auto-approve it without spawning an agent, recording the reason with `--note`:

```bash
tusk review approve <review_id> --note "Skipped: reviewer domains [<reviewer_domains>] does not match task domain [<task_domain>]"
```

Proceed to spawn agents **only for the remaining (non-filtered) review_ids**.

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
- `{reviewer_name}` — the reviewer's `name` field, or "unassigned" if none
- `{reviewer_focus}` — the reviewer's `description` field, or "General code review: correctness, clarity, and consistency." if none
- `{review_categories}` — comma-separated list from config (e.g., `must_fix, suggest, defer`)
- `{review_severities}` — comma-separated list from config (e.g., `critical, major, minor`)

**Do not pass the diff inline.** Each reviewer agent fetches the diff itself via `git diff` (see REVIEWER-PROMPT.md Step 1). This prevents transcription errors from the orchestrator-to-agent copy.

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
   - If **all agents have completed** but some reviews are still `"pending"`, those agents finished without calling `tusk review approve` or `tusk review request-changes`. For each stuck review, log a warning and auto-approve it with a note:
     ```bash
     tusk review approve <review_id> --note "Auto-approved: reviewer agent completed without posting a decision. Most likely cause: Bash tool not permitted in agent sandbox. Required permissions.allow entries: Bash(git diff:*), Bash(git remote:*), Bash(git symbolic-ref:*), Bash(git branch:*), Bash(tusk review:*)"
     ```
     The most common cause is missing Bash tool permissions (the agent could not run `git diff` or `tusk review`). Run `tusk upgrade` to propagate the required `permissions.allow` entries if they are missing from `.claude/settings.json`. Continue as if those reviews returned no findings.

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

These are optional improvements. For each `suggest` comment, **decide autonomously** whether to fix or dismiss — do not ask the user:

- **Fix**: implement the suggestion and run `tusk review resolve <comment_id> fixed`
  - Apply when the fix is small, clearly correct, and within the current task's scope
- **Dismiss**: run `tusk review resolve <comment_id> dismissed`
  - Apply when the suggestion is out of scope, low-value, or would require significant rework

Record every decision (fix or dismiss) with a one-line rationale — these will be included in the final summary so the user can review them.

### defer comments

These are valid issues but out of scope for the current work. For each `defer` comment:

1. Run a heuristic dupe check first:
   ```bash
   tusk dupes check "<summary from comment>" --domain <same domain as current task>
   ```
   Interpret the exit code:
   - **Exit code 0** — no duplicate found; proceed to step 2.
   - **Exit code 1** — duplicate already exists; **skip task creation** and print a note (e.g., "Skipped deferred task — duplicate found: <summary>"). Mark the comment resolved as deferred anyway:
     ```bash
     tusk review resolve <comment_id> deferred
     ```
   - **Any other exit code** — the dupe check itself failed (e.g., database error); **skip task creation**, print a warning (e.g., "Skipped deferred task — dupe check failed with exit code <N>: <summary>"), and mark the comment resolved as deferred.

2. If exit code 0 (no duplicate), create the deferred task:
   ```bash
   tusk task-insert "<summary from comment>" "<full comment text>" \
     --priority Medium \
     --domain <same domain as current task> \
     --task-type refactor \
     --deferred
   ```
   Then mark the comment resolved:
   ```bash
   tusk review resolve <comment_id> deferred
   ```

## Step 8: Re-review Loop (if there were must_fix changes)

If any `must_fix` comments were fixed in Step 7, re-run the review to verify the fixes are correct. Cap the number of passes at `max_passes` from config.

Track current pass number (starts at 1). If `current_pass < max_passes`:

1. Start a new review pass:
   ```bash
   tusk review start <task_id> --pass-num <current_pass + 1> --diff-summary "Re-review pass <n>"
   ```

2. **Verify Bash access before spawning re-review agents.** Run:
   ```bash
   tusk version
   ```
   If this command fails with a permissions error (Bash not permitted), stop and surface:
   > Re-review aborted: Bash tool is not accessible in this session. Reviewer agents require these `permissions.allow` entries in `.claude/settings.json`: `Bash(git diff:*)`, `Bash(git remote:*)`, `Bash(git symbolic-ref:*)`, `Bash(git branch:*)`, `Bash(tusk review:*)`. Run `tusk upgrade` to apply them, then restart the session.

   Spawn reviewer agents only if the command succeeds. Re-review agents fetch the diff themselves — no diff is passed inline.

3. Monitor completion (Step 6) and process findings (Step 7).

4. Increment pass counter. If `current_pass >= max_passes` and there are still open `must_fix` items, **escalate to the user**:
   > Max review passes (<max_passes>) reached. The following must_fix items remain unresolved:
   > <list each open must_fix comment>
   >
   > Please resolve these manually before continuing.

   Stop the re-review loop.

If all `must_fix` items are resolved and no new blocking findings were raised, proceed to Step 9.

## Step 9: Commit Review Fixes

Before summarizing, ensure all changes made during review are committed. Check for any uncommitted modifications:

```bash
git diff --stat
git diff --cached --stat
```

If either command shows output (unstaged or staged changes exist), commit them:

```bash
git add -A
git commit -m "[TASK-<task_id>] Apply review fixes"
git push
```

If the working tree is already clean (no output from either diff command), skip this step.

## Step 10: Final Summary

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
defer:     <total_count> found, <created_count> tasks created, <skipped_count> skipped (duplicate)

Verdict: APPROVED / CHANGES REMAINING
```

The verdict is **APPROVED** if all must_fix comments are resolved (fixed). Otherwise, **CHANGES REMAINING**.
