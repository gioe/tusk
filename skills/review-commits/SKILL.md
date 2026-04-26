---
name: review-commits
description: Run an AI code reviewer against the task's git diff, fix must_fix issues, and defer or dismiss suggestions
allowed-tools: Bash, Read, Task
---

# Review Commits Skill

Orchestrates a single code review against the task's git diff (commits on the current branch vs the base branch). Spawns at most one background reviewer agent (or zero, if no reviewer is configured), monitors completion, fixes must_fix findings, handles suggest findings interactively, and creates deferred tasks for defer findings.

> Use `/create-task` for task creation — handles decomposition, deduplication, criteria, and deps. Use `tusk task-insert` only for bulk/automated inserts.

## Arguments

Optional: `/review-commits <task_id>` — if omitted, task ID is inferred from the current branch name.

---

## Step 0: Start Cost Tracking

First, resolve the task ID so the skill run can be attributed to it. Use the argument if one was passed, otherwise parse it from the current branch:

```bash
tusk branch-parse
```

Returns `{"task_id": N}` on success. If it exits 1 (branch doesn't match pattern) and no argument was passed, ask the user to provide a task ID before continuing. Store the resolved ID as `TASK_ID`.

Then record the start of this review run so cost can be captured at the end:

```bash
tusk skill-run start review-commits --task-id $TASK_ID
```

This prints `{"run_id": N, "started_at": "...", "task_id": N}`. Capture `run_id` — you will need it in Step 11.

> **Early-exit cleanup:** If any validity/mode check below causes the skill to stop before Step 11, first call `tusk skill-run cancel <run_id>` to close the open row, then stop. Otherwise the row lingers as `(open)` in `tusk skill-run list` forever. The explicit cancel calls below cover the known early-exit paths; if you hit an unexpected bail-out, cancel before returning.

## Step 1: Read Config and Check Mode

```bash
tusk config
```

Parse the returned JSON. Extract:
- `review.mode` — if `"disabled"`, run `tusk skill-run cancel <run_id>`, print "Review mode is disabled in config (review.mode = disabled). Enable it in tusk/config.json to use /review-commits." and **stop**.
- `review.max_passes` — maximum fix-and-re-review cycles (default: 2)
- `review.reviewer` — a single reviewer object with `name` and `description` fields, or absent. When absent, the review is created as unassigned and Step 5 falls back to inline review (no agent is spawned).
- `review_categories` — valid comment categories (typically `["must_fix", "suggest", "defer"]`)
- `review_severities` — valid severity levels (typically `["critical", "major", "minor"]`)
- `task_types` — list of valid task type strings. Resolve the best type for deferred tasks now: prefer `"refactor"`, then `"chore"`, then the first entry that is not `"bug"`. Store as `DEFERRED_TASK_TYPE`. If the list is empty or every entry is `"bug"`, set `DEFERRED_TASK_TYPE = null`.

## Step 2: Verify Task and Capture Domain

`TASK_ID` was resolved in Step 0. Verify the task exists and capture its domain:

```bash
tusk -header -column "SELECT id, summary, status, domain FROM tasks WHERE id = $TASK_ID"
```

If no row is returned, run `tusk skill-run cancel <run_id>` to close the open row, then abort: "Task `$TASK_ID` not found."

Store the task's `domain` value — Step 7 uses it when dupe-checking and creating deferred tasks.

## Step 3: Get the Git Diff

Compute the diff range in one call — the helper handles the default-branch resolution (`tusk git-default-branch`), the `<default>...HEAD` primary range, and the `[TASK-<id>]` commit-range recovery fallback used when the feature branch has already been merged and deleted:

```bash
DIFF_RANGE_JSON=$(tusk review-diff-range $TASK_ID)
```

On success the helper prints a single JSON object with four keys (`range`, `diff_lines`, `summary`, `recovered_from_task_commits`) and exits 0. Capture:

```bash
DIFF_RANGE=$(printf '%s' "$DIFF_RANGE_JSON" | jq -r .range)
DIFF_LINES=$(printf '%s' "$DIFF_RANGE_JSON" | jq -r .diff_lines)
DIFF_SUMMARY=$(printf '%s' "$DIFF_RANGE_JSON" | jq -r .summary)
```

> Use `printf '%s'` rather than `echo "$VAR"`. In zsh — and in bash with `xpg_echo` enabled — `echo` interprets the literal `\n` escape sequences inside the captured JSON as real newlines, breaking jq with `Invalid string: control characters from U+0000 through U+001F must be escaped` and silently leaving `$DIFF_SUMMARY` empty.

If the helper exits non-zero, it means no diff is recoverable — either no `[TASK-<id>]` commits were found in recent history, or the recovered range is still empty. The helper's stderr message is the same one Step 3 used to print inline. Run `tusk skill-run cancel <run_id>` and stop, surfacing the helper's stderr verbatim.

Use `$DIFF_RANGE` for any subsequent `git diff` call in this skill, and pass `$DIFF_SUMMARY` to `tusk review start` (Step 4). **Do not pass the diff to reviewer agents** — they will fetch it themselves via `git diff` to avoid transcription errors.

## Step 4: Start the Review

Start a review record for the task. This creates one `code_reviews` row using the configured reviewer (or unassigned if `review.reviewer` is absent):

```bash
tusk review start <task_id> --diff-summary "$DIFF_SUMMARY"
```

`$DIFF_SUMMARY` was captured from the `tusk review-diff-range` JSON in Step 3 — already truncated to the first 120 characters of the diff.

The command prints a single line, for example:

```
Started review #12 for task #42 (reviewer: general): Fix login bug
```

Capture the printed `review_id`.

## Step 5: Spawn the Reviewer Agent

Only when the diff is non-empty and a review has been started in Step 4, proceed with the steps below.

### Step 5.1: Choose review strategy and verify permissions

> **Important:** Background reviewer agents run in an **isolated sandbox** and do **not** inherit the parent session's tool permissions. Approving Bash in this conversation does not grant Bash access to spawned agents. The `permissions.allow` block in `.claude/settings.json` is the only reliable way to grant tool access in agent sandboxes — it applies to all subagents spawned from this project, regardless of what is auto-approved in the current session.

**Inline-review path (no agent spawned).** Use the inline path when *any* of the following is true:
- The diff is small (fewer than ~200 lines) or contains only non-code files (`.md`, `.json`, `.yaml`).
- `review.reviewer` is absent from config (the review record is unassigned and no agent is configured to handle it).

Read the diff yourself, evaluate it, and record the result directly. Always pass `--model <your_model_id>` — the canonical ID matching the format in `task_sessions.model` (e.g. `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`). Strip any suffixes like `[1m]` or date-stamps from your system prompt's ID so the value joins cleanly against other model-tagged tables (e.g. `claude-opus-4-7[1m]` → `claude-opus-4-7`):

```bash
# Approve with no findings:
tusk review approve <review_id> --model <your_model_id> --note "Inline review: small/docs-only diff (or no reviewer configured), no findings."
# Or if changes are needed:
tusk review request-changes <review_id> --model <your_model_id>
# Then add comments as needed:
tusk review add-comment <review_id> "<description>" --file "<file>" --line-start <line> --category <category> --severity <severity>
```

After recording the inline decision, skip directly to Step 7.

**Agent path.** For larger code diffs with a configured reviewer, verify the required agent sandbox permissions before spawning the reviewer agent:

```bash
REVIEW_PERM_CHECK=$(tusk review-check-perms) || { echo "Agent review aborted: $REVIEW_PERM_CHECK"; tusk skill-run cancel <run_id>; exit 1; }
```

On success the command prints `OK` and exits 0. On failure it prints a single `MISSING: …` line (either `not found on disk or in HEAD`, a JSON/shape error, or a comma-separated list of missing `permissions.allow` entries), cancels the skill run to avoid an orphan pending row, and exits 1. When the check fails, surface to the user:
> Agent review aborted: `<captured MISSING: line>`. Create `.claude/settings.json` or add the missing entries manually, or run `tusk upgrade` to apply them, then restart the session.

Proceed to spawn the agent only if the check prints `OK`.

Read the reviewer prompt template:

```
Read file: <base_directory>/REVIEWER-PROMPT.md
```

Where `<base_directory>` is the skill base directory shown at the top of this file.

Spawn a single **background agent** using the Task tool:

```
Task tool call:
  description: "review-commits reviewer task <task_id>"
  subagent_type: general-purpose
  run_in_background: true
  prompt: <REVIEWER-PROMPT.md content, with placeholders replaced — see template>
```

Fill in these placeholders from the template:
- `{task_id}` — the task ID
- `{review_id}` — the review ID captured in Step 4
- `{reviewer_name}` — `review.reviewer.name` from config
- `{reviewer_focus}` — `review.reviewer.description` from config
- `{review_categories}` — comma-separated list from config (e.g., `must_fix, suggest, defer`)
- `{review_severities}` — comma-separated list from config (e.g., `critical, major, minor`)

**Do not pass the diff inline.** The reviewer agent fetches the diff itself via `git diff` (see REVIEWER-PROMPT.md Step 1). This prevents transcription errors from the orchestrator-to-agent copy.

After spawning, record the agent task ID.

## Step 6: Monitor Reviewer Completion

Wait for the reviewer agent to finish. The agent was spawned with `run_in_background: true` in Step 5, so the runtime emits an automatic completion notification when the agent exits. **Do not chain `sleep 30 && tusk review status <task_id>`** — the runtime blocks long leading sleeps and emits a tool error every time, even though the run still completes via the auto-notification.

**Primary path: wait for the auto-completion notification.**

No active polling required — the runtime delivers a notification when the background agent exits. When it arrives, fall through to the **Resolve the verdict** sub-step below.

**Stall detection (no notification within ~2.5 min):**

If you have been waiting for the agent without a completion notification for ~2.5 minutes (matching the previous `STALL_THRESHOLD = 5 × 30s` semantics), the agent may be looping or running a long-running command. Use a short-sleep until-loop — the runtime sleep guard allows `sleep 2` inside an `until` body — that exits as soon as `tusk review status` returns a terminal verdict OR the wall-clock deadline elapses:

```bash
DEADLINE=$(($(date +%s) + 150))
until [ "$(tusk review status <task_id> | jq -r .status)" != "pending" ] || [ "$(date +%s)" -ge "$DEADLINE" ]; do
  sleep 2
done
```

After the loop exits, fall through to the **Resolve the verdict** sub-step.

**Resolve the verdict:**

Re-read the review status and decide how to proceed:

```bash
tusk review status <task_id>
```

Parse the JSON.

- **`status` is `"approved"` or `"changes_requested"`** → proceed to Step 7.

- **`status` is still `"pending"`** → check whether the agent has finished using `TaskOutput` with `block: false` and the agent task ID:

  **Agent has completed** (TaskOutput shows the agent is done) but the review is still `"pending"`:
  - The agent finished without calling `tusk review approve` or `tusk review request-changes`. Log a warning and auto-approve with a note. Pass `--model <your_model_id>` (the orchestrator's own ID from its system prompt) since the orchestrator, not the silent agent, is closing this review:
    ```bash
    tusk review approve <review_id> --model <your_model_id> --note "Auto-approved (no verdict): reviewer agent completed without posting a decision. Most likely cause: Bash tool not permitted in agent sandbox. Required permissions.allow entries: Bash(git diff:*), Bash(git remote:*), Bash(git symbolic-ref:*), Bash(git branch:*), Bash(tusk review:*)"
    ```
    The most common cause is missing Bash tool permissions (the agent could not run `git diff` or `tusk review`). Run `tusk upgrade` to propagate the required `permissions.allow` entries if they are missing from `.claude/settings.json`. Continue as if the review returned no findings.

  **Agent is still running** after the stall deadline elapsed:
  - Auto-approve with a stall warning note. Pass `--model <your_model_id>` (the orchestrator's own ID) since the orchestrator, not the stalled agent, is closing this review:
    ```bash
    tusk review approve <review_id> --model <your_model_id> --note "Auto-approved (stall): reviewer agent has been running for ≥2.5 min without posting a verdict. The agent may be looping or running a long-running command such as a full test suite. Check REVIEWER-PROMPT.md Step 2.6 constraints. To prevent stalls, ensure the agent sandbox has the required permissions.allow entries: Bash(git diff:*), Bash(git remote:*), Bash(git symbolic-ref:*), Bash(git branch:*), Bash(tusk review:*)"
    ```
    Continue as if the review returned no findings.

## Step 7: Process Findings

After the reviewer agent completes, fetch the full review results:

```bash
tusk review list <task_id>
```

Gather all open (unresolved) comments from the review. Before processing any comments, initialize a bash array to track every file you touch during review fixes — Step 9 uses this list to stage only the files you actually modified:

```bash
REVIEW_FIX_FILES=()
```

Group the open comments by category:

### must_fix comments

These are blocking issues that must be resolved before the work can be merged.

For each open `must_fix` comment:
1. Read the comment details (file path, line numbers, comment text, severity).
2. Implement the fix directly in the codebase.
3. Record every file you modified while addressing this comment — usually the comment's own `file_path`, plus any additional files the fix required (new tests, helper extraction, etc.):
   ```bash
   REVIEW_FIX_FILES+=("<file_path>")
   ```
4. After fixing, mark the comment resolved:
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

- **Fix**: implement the suggestion, append every file you modified to `REVIEW_FIX_FILES` (`REVIEW_FIX_FILES+=("<file_path>")`), then run `tusk review resolve <comment_id> fixed`
  - Apply when the fix is small, clearly correct, and within the current task's scope
- **Dismiss**: run `tusk review resolve <comment_id> dismissed`
  - Apply when the suggestion is out of scope, low-value, or would require significant rework

Record every decision (fix or dismiss) with a one-line rationale — these will be included in the final summary so the user can review them.

### defer comments

These are valid issues but out of scope for the current work. If `DEFERRED_TASK_TYPE` (resolved in Step 1) is **null** — config has no suitable task type — skip helper invocation entirely. For each `defer` comment, print a warning "Skipped deferred task — no suitable task_type in config (not 'bug'): <summary>" and mark the comment resolved manually via `tusk review resolve <comment_id> deferred`.

Otherwise, call the helper per comment to atomically run the dupe check, insert the deferred task (when not a duplicate), and mark the comment resolved — one call replaces the prior three-step dance:

```bash
tusk review-defer <comment_id> --domain <same domain as current task> --task-type <DEFERRED_TASK_TYPE>
```

The helper reads the comment text from `review_comments`, runs `tusk dupes check` on the derived summary against the given domain, and:
- inserts a new deferred task (`--priority Medium`, `--task-type <DEFERRED_TASK_TYPE>`, `--deferred`, criterion "Address deferred finding: <summary>") when there is no duplicate;
- records the match and skips insertion when a duplicate already exists;
- records the failure and skips insertion when the dupe check itself errored.

In all three branches the comment is marked resolved. The helper exits 0 and prints JSON `{created_task_id, skipped_reason, matched_task_id}` on stdout:
- `created_task_id` set, `skipped_reason` null — new deferred task was created; note the id.
- `skipped_reason: "duplicate"` — `matched_task_id` points at an open task already covering this finding; print a note (e.g., "Skipped deferred task — duplicate of #<id>: <summary>").
- `skipped_reason: "dupe_check_failed"` — the dupe check itself errored; print a warning (e.g., "Skipped deferred task — dupe check failed: <summary>") so the user can re-file manually if needed.

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

2. **Check diff size before deciding review strategy.** Recompute the range with the same helper used in Step 3 — it transparently handles both the default-branch (TASK-commit recovery) and feature-branch (`<default>...HEAD`) cases:

   ```bash
   DIFF_LINES=$(tusk review-diff-range $TASK_ID | jq -r .diff_lines)
   ```

   **For small or documentation-only diffs (`$DIFF_LINES` below ~200, or only non-code files), or when `review.reviewer` is absent from config:** skip agent spawning and perform an inline review. Read the diff yourself, evaluate it against the reviewer focus area, and record the result directly (approve or request-changes + add-comment). After recording the inline decision, skip to step 3.

   **For all other diffs:** verify the required agent sandbox permissions are configured before spawning the re-review agent. Run:

   ```bash
   REVIEW_PERM_CHECK=$(tusk review-check-perms) || { echo "Re-review agent aborted: $REVIEW_PERM_CHECK"; exit 1; }
   ```

   On failure the command prints a single `MISSING: …` line and exits 1. When the check fails, surface to the user:
   > Re-review agent aborted: `<captured MISSING: line>`. Create `.claude/settings.json` or add the missing entries manually, or run `tusk upgrade` to apply them, then restart the session.

   Proceed to spawn the re-review agent only if the check prints `OK`. The re-review agent fetches the diff itself — no diff is passed inline.

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

If both commands show no output, the working tree is clean — skip this step.

Otherwise, commit **only** the files you tracked in `REVIEW_FIX_FILES` during Steps 7 and 8. **Never use `git add -A` or `git add .`** — those stage every dirty or untracked file in the working tree, including unrelated changes from other sessions (a real incident on TASK-1423 produced a 460-file commit that had to be reverted twice).

First, deduplicate the tracked list and reconcile it against the actual diff **before** staging or committing:

```bash
# Deduplicate the tracked file list
REVIEW_FIX_FILES=($(printf '%s\n' "${REVIEW_FIX_FILES[@]}" | sort -u))

# Abort if no files were tracked but a diff exists — investigate manually
if [ ${#REVIEW_FIX_FILES[@]} -eq 0 ]; then
  echo "ERROR: uncommitted changes exist but REVIEW_FIX_FILES is empty. Review the diff above and stage files explicitly by name." >&2
  exit 1
fi
```

Now re-run `git diff --stat` and `git diff --cached --stat` and compare the listed paths to `REVIEW_FIX_FILES`. If any path you *did* modify during review is missing from the array, append it explicitly by name (never fall back to `git add -A`):

```bash
REVIEW_FIX_FILES+=("<path-you-modified>")
```

Conversely, any remaining unstaged paths that are **not** in `REVIEW_FIX_FILES` must be scratch work from other sessions — leave them alone.

Once the list is reconciled, stage, commit, and push in a single pass:

```bash
git add -- "${REVIEW_FIX_FILES[@]}"
git commit -m "[TASK-<task_id>] Apply review fixes"
git push --set-upstream origin HEAD
```

`--set-upstream origin HEAD` is required on the **first** push of a brand-new feature branch when `push.autoSetupRemote` is not set in the user's git config — bare `git push` aborts with "no upstream branch". The flag is idempotent on subsequent pushes (just re-binds the existing tracking ref), so it is safe to use unconditionally.

## Step 10: Final Summary

Render the final summary block in one call — the helper reads all counts from `code_reviews` / `review_comments`, computes the verdict the same way as `tusk review verdict`, and maps `APPROVED` / `CHANGES_REMAINING` to the display label (`APPROVED` / `CHANGES REMAINING`):

```bash
tusk review-final-summary <review_id>
```

Output shape:

```
Review complete for Task <task_id>: <task_summary>
══════════════════════════════════════════════════
Pass:      <pass number of this review>

must_fix:  <total_count> found, <fixed_count> fixed
suggest:   <total_count> found, <fixed_count> fixed, <dismissed_count> dismissed
defer:     <total_count> found, <created_count> tasks created, <skipped_count> skipped (duplicate)

Verdict: <APPROVED | CHANGES REMAINING>
```

Counts aggregate across **all** of the task's reviews (including superseded passes) so the block reflects cumulative findings — but the verdict considers only non-superseded reviews, matching `tusk review verdict`. A deferred finding counts as "created" when `review_comments.deferred_task_id` is populated and as "skipped" (duplicate) when it is NULL.

## Step 11: Finish Cost Tracking

Record cost for this review run. Replace `<run_id>` with the value captured in Step 0, and fill in the actual counts from this run:

- `must_fix_count` — the `open_must_fix` value from `tusk review-verdict` in Step 10.
- `passes` — the final pass number printed in Step 10's summary block.
- `diff_lines` — the `DIFF_LINES` value captured in Step 3.

```bash
tusk skill-run finish <run_id> --metadata '{"must_fix_count":<M>,"passes":<P>,"diff_lines":<D>}'
```

This reads the Claude Code transcript for the time window of this run and stores token counts and estimated cost in the `skill_runs` table.

To view cost history across all review-commits runs:

```bash
tusk skill-run list review-commits
```
