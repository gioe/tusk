---
name: review-commits
description: Run parallel AI code reviewers against the task's git diff, fix must_fix issues, and defer or dismiss suggestions
allowed-tools: Bash, Read, Task
---

# Review Commits Skill

Orchestrates parallel code review against the task's git diff (commits on the current branch vs the base branch). Spawns one background reviewer agent per enabled reviewer in config, monitors completion, fixes must_fix findings, handles suggest findings interactively, and creates deferred tasks for defer findings.

> Use `/create-task` for task creation — handles decomposition, deduplication, criteria, and deps. Use `tusk task-insert` only for bulk/automated inserts.

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
- `task_types` — list of valid task type strings. Resolve the best type for deferred tasks now: prefer `"refactor"`, then `"chore"`, then the first entry that is not `"bug"`. Store as `DEFERRED_TASK_TYPE`. If the list is empty or every entry is `"bug"`, set `DEFERRED_TASK_TYPE = null`.

## Step 2: Detect Task ID

If a task ID was passed as an argument, use it. Otherwise, infer from the current branch:

```bash
tusk branch-parse
```

Returns `{"task_id": N}` on success. If the command exits 1 (branch doesn't match pattern), ask the user to provide a task ID.

Verify the task exists and capture its domain:

```bash
tusk -header -column "SELECT id, summary, status, domain FROM tasks WHERE id = <task_id>"
```

If no row is returned, abort: "Task `<task_id>` not found."

Store the task's `domain` value (may be NULL/empty — this is used to filter reviewers in Step 5).

## Step 3: Get the Git Diff

Determine the base branch and compute the diff:

```bash
DEFAULT_BRANCH=$(tusk git-default-branch)
CURRENT_BRANCH=$(git branch --show-current)
git diff "${DEFAULT_BRANCH}...HEAD"
```

If the diff is empty — whether on the default branch or on a feature branch whose commits have already been merged (fast-forward or otherwise) — attempt to recover the correct range by scanning git log for `[TASK-<id>]` commits (where `<id>` is `TASK_ID` from Step 2):

```bash
TASK_COMMITS=$(git log --format="%H" --grep="\[TASK-${TASK_ID}\]" -n 50)
```

If `TASK_COMMITS` is non-empty, construct the range from the oldest to the newest matching commit:

```bash
NEWEST_COMMIT=$(echo "$TASK_COMMITS" | head -1)
OLDEST_COMMIT=$(echo "$TASK_COMMITS" | tail -1)
git diff "${OLDEST_COMMIT}^..${NEWEST_COMMIT}"
```

Use this diff (and this range) going forward — including for the `--diff-summary` passed to `tusk review start` and for any re-review diff stat checks in Step 8.

If `TASK_COMMITS` is empty (no `[TASK-<id>]` commits found in recent history), stop with:
> No changes found — `[TASK-<task_id>]` commits not detected in recent git log. The diff range cannot be determined automatically. Confirm the correct commit range manually and re-run.

If the diff is still empty after the TASK-commit recovery, report "No changes found compared to the base branch." and stop.

Capture the diff only to check for emptiness and to generate the `--diff-summary` for `tusk review start`. **Do not pass the diff to reviewer agents** — they will fetch it themselves via `git diff` to avoid transcription errors.

## Step 4: Start Reviews

Start a review record for the task. This creates one `code_reviews` row per configured reviewer (or one unassigned row if no reviewers are configured):

```bash
tusk review start <task_id> --diff-summary "<first 120 chars of diff summary>"
```

The command prints **one line per created review**. Example output for 3 configured reviewers:

```
Started review #12 for task #42 (reviewer: general): Fix login bug
Started review #13 for task #42 (reviewer: backend): Fix login bug
Started review #14 for task #42 (reviewer: security): Fix login bug
```

Parse **every line** to collect all review IDs. Do not stop after the first line.
Store the mapping: reviewer name → review_id (e.g., `{"general": 12, "backend": 13, "security": 14}`).

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
tusk review add-comment <review_id> "<description>" --file "<file>" --line-start <line> --category <category> --severity <severity>
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

Run:

```bash
tusk filter-reviewers --task-id <task_id>
```

This returns a JSON array of reviewer names that match the task's domain (e.g. `["general", "security"]`). Use it together with the `reviewer name → review_id` mapping from Step 4 to determine which review_ids to spawn agents for.

For each review_id whose reviewer name is **not** in the returned array, immediately auto-approve it without spawning an agent, recording the reason with `--note`:

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

**Setup before entering the loop:**

Initialize a stall counter for each spawned review_id:
```
stall_counts = { <review_id>: 0, ... }   # one entry per spawned agent
STALL_THRESHOLD = 5                       # iterations (~2.5 min at 30 s/iter)
```

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

   **Agent still running:**
   - Increment `stall_counts[review_id]` by 1.
   - If `stall_counts[review_id] >= STALL_THRESHOLD`, the agent has been running for too long without posting a verdict. Auto-approve it immediately with a stall warning note and remove it from the pending set:
     ```bash
     tusk review approve <review_id> --note "Auto-approved (stall): reviewer agent has been running for ≥5 monitoring iterations (~2.5 min) without posting a verdict. The agent may be looping or running a long-running command such as a full test suite. Check REVIEWER-PROMPT.md Step 2.6 constraints. To prevent stalls, ensure the agent sandbox has the required permissions.allow entries: Bash(git diff:*), Bash(git remote:*), Bash(git symbolic-ref:*), Bash(git branch:*), Bash(tusk review:*)"
     ```
     Continue as if this review returned no findings.

   **Agent has completed** (TaskOutput shows the agent is done) but the review is still `"pending"`:
   - The agent finished without calling `tusk review approve` or `tusk review request-changes`. Log a warning and auto-approve it with a note:
     ```bash
     tusk review approve <review_id> --note "Auto-approved (no verdict): reviewer agent completed without posting a decision. Most likely cause: Bash tool not permitted in agent sandbox. Required permissions.allow entries: Bash(git diff:*), Bash(git remote:*), Bash(git symbolic-ref:*), Bash(git branch:*), Bash(tusk review:*)"
     ```
     The most common cause is missing Bash tool permissions (the agent could not run `git diff` or `tusk review`). Run `tusk upgrade` to propagate the required `permissions.allow` entries if they are missing from `.claude/settings.json`. Continue as if those reviews returned no findings.

4. If any agents remain running (and have not yet been stall-auto-approved), go back to step 1.

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

2. If exit code 0 (no duplicate), create the deferred task. Use `DEFERRED_TASK_TYPE` resolved in Step 1:
   - **If `DEFERRED_TASK_TYPE` is non-null**, include `--task-type <DEFERRED_TASK_TYPE>`:
     ```bash
     tusk task-insert "<summary from comment>" "<full comment text>" \
       --priority Medium \
       --domain <same domain as current task> \
       --task-type <DEFERRED_TASK_TYPE> \
       --deferred
     ```
   - **If `DEFERRED_TASK_TYPE` is null** (config has no suitable task type), skip task creation and print a warning: "Skipped deferred task — no suitable task_type in config (not 'bug'): <summary>".

   In both cases, mark the comment resolved:
   ```bash
   tusk review resolve <comment_id> deferred
   ```

After processing all findings, check the current verdict:

```bash
tusk review-verdict <task_id>
```

This returns `{"verdict": "APPROVED|CHANGES_REMAINING", "open_must_fix": N}`. If `verdict` is `APPROVED` and no `must_fix` changes were made, skip Step 8 and proceed directly to Step 9.

## Step 8: Re-review Loop (if there were must_fix changes)

If any `must_fix` comments were fixed in Step 7, re-run the review to verify the fixes are correct. Check pass status before starting:

```bash
tusk review-pass-status <task_id>
```

This returns `{"current_pass": N, "max_passes": N, "can_retry": bool, "open_must_fix": N}`.

If `can_retry` is false (either no open `must_fix` items, or `current_pass >= max_passes`), do not enter the loop. If `open_must_fix > 0` and `can_retry` is false, **escalate to the user**:
> Max review passes (`max_passes`) reached. The following must_fix items remain unresolved:
> <list each open must_fix comment>
>
> Please resolve these manually before continuing.

Otherwise, loop while `can_retry` is true:

1. Start a new review pass:
   ```bash
   tusk review start <task_id> --pass-num <current_pass + 1> --diff-summary "Re-review pass <n>"
   ```

2. **Check diff size before deciding review strategy.** Measure the current diff using the same range established in Step 3:
   - If `CURRENT_BRANCH == DEFAULT_BRANCH` (on default branch), use the TASK-commit range from Step 3:
     ```bash
     git diff "${OLDEST_COMMIT}^..${NEWEST_COMMIT}" --stat | tail -1
     ```
   - Otherwise (feature branch), use the merge-base range:
     ```bash
     DEFAULT_BRANCH=$(tusk git-default-branch); git diff $(git merge-base HEAD origin/${DEFAULT_BRANCH})..HEAD --stat | tail -1
     ```

   **For small or documentation-only diffs (fewer than ~200 lines changed, or only non-code files):** skip agent spawning and perform an inline review. Read the diff yourself, evaluate it against reviewer focus areas, and record the result directly (approve or request-changes + add-comment). After recording the inline decision, skip to step 3.

   **For all other diffs:** verify Bash is accessible before spawning re-review agents. Run:

   ```bash
   tusk version
   ```

   If this command fails with a permissions error (Bash not permitted), stop and surface:
   > Re-review agent aborted: Bash is not accessible in agent sandboxes for this project. The `permissions.allow` block in `.claude/settings.json` must include the required entries: `Bash(git diff:*)`, `Bash(git remote:*)`, `Bash(git symbolic-ref:*)`, `Bash(git branch:*)`, `Bash(tusk review:*)`. Run `tusk upgrade` to apply them, then restart the session.

   Proceed to spawn re-review agents only if `tusk version` succeeds. Re-review agents fetch the diff themselves — no diff is passed inline.

3. Monitor completion (Step 6) and process findings (Step 7).

4. Re-check pass status to determine whether to continue:
   ```bash
   tusk review-pass-status <task_id>
   ```
   If `can_retry` is still true and `open_must_fix > 0`, repeat from step 1.
   If `can_retry` is false and `open_must_fix > 0`, **escalate to the user** (same message as above).

If `tusk review-verdict <task_id>` returns `"verdict": "APPROVED"` and no new blocking findings were raised, proceed to Step 9.

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

Then print an overall summary. Retrieve the verdict:

```bash
tusk review-verdict <task_id>
```

Use the returned `verdict` and `open_must_fix` values in the summary. Map the machine-readable verdict to a human-readable label before printing:

| `verdict` value     | Display label      |
|---------------------|--------------------|
| `APPROVED`          | `APPROVED`         |
| `CHANGES_REMAINING` | `CHANGES REMAINING`|

```
Review complete for Task <task_id>: <task_summary>
══════════════════════════════════════════════════
Reviewers: <count>
Pass:      <final_pass_number>

must_fix:  <total_count> found, <fixed_count> fixed
suggest:   <total_count> found, <fixed_count> fixed, <dismissed_count> dismissed
defer:     <total_count> found, <created_count> tasks created, <skipped_count> skipped (duplicate)

Verdict: <display label>
```
