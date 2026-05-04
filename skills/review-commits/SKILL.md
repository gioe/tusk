---
name: review-commits
description: Run an AI code reviewer against the task's git diff, fix must_fix issues, and dismiss or spin off suggestions
allowed-tools: Bash, Read, Task
---

# Review Commits Skill

Orchestrates a single code review against the task's git diff (commits on the current branch vs the base branch). Spawns at most one background reviewer agent (or zero, if no reviewer is configured), monitors completion, fixes must_fix findings, and handles suggest findings interactively (fix now, spin off into a follow-up task, or dismiss).

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
- `review_categories` — valid comment categories (typically `["must_fix", "suggest"]`)
- `review_severities` — valid severity levels (typically `["critical", "major", "minor"]`)
- `task_types` — list of valid task type strings. Resolve the best type for follow-up tasks created from `suggest` findings now: prefer `"refactor"`, then `"chore"`, then the first entry that is not `"bug"`. Store as `FOLLOWUP_TASK_TYPE`. If the list is empty or every entry is `"bug"`, set `FOLLOWUP_TASK_TYPE = null`.

## Step 2: Verify Task and Capture Domain

`TASK_ID` was resolved in Step 0. Verify the task exists and capture its domain:

```bash
tusk -header -column "SELECT id, summary, status, domain FROM tasks WHERE id = $TASK_ID"
```

If no row is returned, run `tusk skill-run cancel <run_id>` to close the open row, then abort: "Task `$TASK_ID` not found."

Store the task's `domain` value — Step 7 uses it when dupe-checking and creating follow-up tasks from `suggest` findings.

## Step 3: Compute Diff Range and Start the Review

Bundle the diff-range computation and the `code_reviews` row creation into one call. The helper handles the default-branch resolution (`tusk git-default-branch`), the `<default>...HEAD` primary range, the `[TASK-<id>]` commit-range recovery fallback used when the feature branch has already been merged and deleted, and stamps the captured diff summary onto the new review row internally — so the dangerous summary string never has to round-trip through shell variables:

```bash
REVIEW_BEGIN_JSON=$(tusk review begin $TASK_ID)
```

On success the helper prints a single JSON object with `review_id`, `task_id`, `reviewer`, `range`, `diff_lines`, and `recovered_from_task_commits`, and exits 0. Capture:

```bash
REVIEW_ID=$(printf '%s' "$REVIEW_BEGIN_JSON" | jq -r .review_id)
DIFF_RANGE=$(printf '%s' "$REVIEW_BEGIN_JSON" | jq -r .range)
DIFF_LINES=$(printf '%s' "$REVIEW_BEGIN_JSON" | jq -r .diff_lines)
```

If the helper exits non-zero, it means no diff is recoverable — either no `[TASK-<id>]` commits were found in recent history, or the recovered range is still empty. The helper's stderr message is the same one Step 3 used to print inline. Run `tusk skill-run cancel <run_id>` and stop, surfacing the helper's stderr verbatim.

Use `$DIFF_RANGE` for any subsequent `git diff` call in this skill. **Do not pass the diff to reviewer agents** — they will fetch it themselves via `git diff` to avoid transcription errors.

## Step 5: Spawn the Reviewer Agent

Only when the diff is non-empty and a review has been started in Step 3, proceed with the steps below.

### Step 5.1: Choose review strategy and verify permissions

> **Important:** Background reviewer agents run in an **isolated sandbox** and do **not** inherit the parent session's tool permissions. Approving Bash in this conversation does not grant Bash access to spawned agents. The `permissions.allow` block in `.claude/settings.json` is the only reliable way to grant tool access in agent sandboxes — it applies to all subagents spawned from this project, regardless of what is auto-approved in the current session.

**Inline-review path (no agent spawned).** Use the inline path when *any* of the following is true:
- The diff is small (fewer than ~200 lines) or contains only non-code files (`.md`, `.json`, `.yaml`).
- `review.reviewer` is absent from config (the review record is unassigned and no agent is configured to handle it).
- Tusk is running under a Codex install AND the user did not explicitly opt into subagent-based review for this `/review-commits` invocation. Codex session policy disallows spawning subagents unless the operator asks for one, so the inline path is the safe default — it keeps the real-diff review workflow without violating session policy.

**Detecting Codex install mode and the opt-in.** Read the `install-mode` marker stamped by `install.sh` (Claude installs are marked `claude-…`; Codex installs are marked `codex-…`):

```bash
TUSK_BIN_DIR="$(dirname "$(command -v tusk)")"
INSTALL_MODE="$(tr -d '[:space:]' < "$TUSK_BIN_DIR/install-mode" 2>/dev/null || echo claude-source)"
case "$INSTALL_MODE" in codex-*) IS_CODEX=1 ;; *) IS_CODEX=0 ;; esac
```

Treat the user as having opted into the agent path only when their `/review-commits` invocation explicitly contains a phrase like `use the reviewer agent`, `delegate review`, `spawn the reviewer`, or `agent review`. A bare `/review-commits` (or one with only a task ID argument) is **not** an opt-in. When `IS_CODEX=1` and no opt-in phrase is present, surface the routing decision before reading the diff — e.g. *"Codex install detected — running inline review. Re-run with `use the reviewer agent` to opt into agent-based review."* — so the operator can re-invoke with the opt-in flag if they want a full agent review.

**Why install-mode and not a runtime signal?** Codex (the `openai/codex` CLI) does not document or inject a `CODEX_*` env var into subprocess environments to mark "running under Codex" — `CODEX_HOME` is a configuration input pointing at Codex's local state, not an output marker, and `shell_environment_policy` lets users strip inherited variables freely (so even if a marker existed, it would not be reliable). install-mode is therefore the most durable signal we have: `install.sh` chooses it from `.claude/` (claude) vs `AGENTS.md`-only (codex) at install time and stamps the marker once. **Mixed-mode caveat:** a repo with both `.claude/` and `AGENTS.md` is marked `claude-*` by `install.sh`. If `/review-commits` is invoked from a Codex session in such a repo, `IS_CODEX=0` and the agent path is taken — under Codex's subagent policy that spawn may fail. If it does, perform a manual inline review: read the diff yourself, then use `tusk review approve` or `tusk review request-changes` + `tusk review add-comment`.

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

**Capture cost-attribution anchors before spawning.** The reviewer agent runs in its own Task sandbox and writes to a separate `<session-uuid>.jsonl` under `~/.claude/projects/<project-hash>/`. The orchestrator's auto-compute path uses `find_transcript()`, which returns whichever JSONL has the most recent mtime — typically the orchestrator's own (continuously updated by tool results), so the cost recorded on `code_reviews` reflects orchestrator wait time, not the actual reviewer-agent spend. To attribute correctly, snapshot the orchestrator's JSONL path and the spawn timestamp now, before spawning:

```bash
ORCH_JSONL=$(tusk review-agent-cost --print-orchestrator-jsonl)
SPAWN_TS=$(date +%s)
```

Hold both values for Step 6, where the orchestrator runs `tusk review-agent-cost --since "$SPAWN_TS" --exclude-jsonl "$ORCH_JSONL"` after the agent completes and pipes the result into `tusk review backfill-cost --force <review_id> --cost-dollars X --tokens-in Y --tokens-out Z`. If `tusk review-agent-cost --print-orchestrator-jsonl` exits non-zero (no transcript found), fall through without setting `ORCH_JSONL` — Step 6 will skip the cost-correction step and the row keeps its (orchestrator-only) auto-compute.

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
- `{review_id}` — the review ID captured in Step 3
- `{reviewer_name}` — `review.reviewer.name` from config
- `{reviewer_focus}` — `review.reviewer.description` from config
- `{review_categories}` — comma-separated list from config (e.g., `must_fix, suggest`)
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

- **`status` is `"approved"` or `"changes_requested"`** → the agent posted its verdict normally. Now correct the cost attribution before moving on (see "Apply agent cost" below), then proceed to Step 7.

- **`status` is still `"pending"`** → check whether the agent has finished using `TaskOutput` with `block: false` and the agent task ID:

  **Agent has completed** (TaskOutput shows the agent is done) but the review is still `"pending"`:
  - The agent finished without calling `tusk review approve` or `tusk review request-changes`. Log a warning and auto-approve with a note. Pass `--model <your_model_id>` (the orchestrator's own ID from its system prompt) since the orchestrator, not the silent agent, is closing this review. **Cost note:** because the orchestrator is closing the review, the row's `cost_dollars` is auto-computed from the orchestrator's transcript window and reflects only orchestrator-side spend (the agent never recorded a verdict, so its API tokens cannot be attributed via the normal flow). After the approve call, attempt the agent-cost correction below — the agent did exit, so its JSONL may exist:
    ```bash
    tusk review approve <review_id> --model <your_model_id> --note "Auto-approved (no verdict): reviewer agent completed without posting a decision. Most likely cause: Bash tool not permitted in agent sandbox. Required permissions.allow entries: Bash(git diff:*), Bash(git remote:*), Bash(git symbolic-ref:*), Bash(git branch:*), Bash(tusk review:*)"
    ```
    The most common cause is missing Bash tool permissions (the agent could not run `git diff` or `tusk review`). Run `tusk upgrade` to propagate the required `permissions.allow` entries if they are missing from `.claude/settings.json`. Continue as if the review returned no findings.

  **Agent is still running** after the stall deadline elapsed:
  - Auto-approve with a stall warning note. Pass `--model <your_model_id>` (the orchestrator's own ID) since the orchestrator, not the stalled agent, is closing this review. **Cost note:** the row's `cost_dollars` here reflects orchestrator-only attribution — the agent is still mid-run, so its in-progress JSONL is not safe to aggregate. **Skip the agent-cost correction** in this branch and accept the orchestrator-side cost; document the gap with the stall note already on the row:
    ```bash
    tusk review approve <review_id> --model <your_model_id> --note "Auto-approved (stall): reviewer agent has been running for ≥2.5 min without posting a verdict. The agent may be looping or running a long-running command such as a full test suite. Check REVIEWER-PROMPT.md Step 2.6 constraints. To prevent stalls, ensure the agent sandbox has the required permissions.allow entries: Bash(git diff:*), Bash(git remote:*), Bash(git symbolic-ref:*), Bash(git branch:*), Bash(tusk review:*)"
    ```
    Continue as if the review returned no findings.

**Apply agent cost (normal-completion path only).** When the agent posted its verdict normally — i.e. the `status` check above returned `"approved"` or `"changes_requested"` — its `tusk review approve` / `tusk review request-changes` call ran inside the agent sandbox and the auto-compute resolved against `find_transcript()`, which (because the orchestrator's JSONL is being continuously updated) typically attributed to the orchestrator's transcript window. Override the row with the agent's actual spend now:

```bash
if [ -n "$ORCH_JSONL" ]; then
  AGENT_COST_JSON=$(tusk review-agent-cost --since "$SPAWN_TS" --exclude-jsonl "$ORCH_JSONL")
  AGENT_COST_RC=$?
  if [ "$AGENT_COST_RC" -eq 0 ]; then
    AGENT_COST=$(printf '%s' "$AGENT_COST_JSON" | jq -r .cost_dollars)
    AGENT_TIN=$(printf '%s' "$AGENT_COST_JSON"  | jq -r .tokens_in)
    AGENT_TOUT=$(printf '%s' "$AGENT_COST_JSON" | jq -r .tokens_out)
    tusk review backfill-cost --force "$REVIEW_ID" \
      --cost-dollars "$AGENT_COST" --tokens-in "$AGENT_TIN" --tokens-out "$AGENT_TOUT"
  fi
fi
```

`tusk review-agent-cost` reads the project's Claude transcripts dir, lists JSONLs modified at or after `$SPAWN_TS`, excludes `$ORCH_JSONL`, and aggregates token usage and cost across the remaining (agent) transcripts. Exit 0 means the override flags carry the agent's actual spend; exit 1 means no agent transcripts were discoverable (subagent JSONLs may live elsewhere on this host) and the row keeps its (orchestrator-only) auto-compute. Skip the block entirely if `$ORCH_JSONL` was not captured in Step 5.1.

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

These are optional improvements. For each `suggest` comment, **decide autonomously** between three branches — do not ask the user:

- **Fix**: implement the suggestion, append every file you modified to `REVIEW_FIX_FILES` (`REVIEW_FIX_FILES+=("<file_path>")`), then run `tusk review resolve <comment_id> fixed`
  - Apply when the fix is small, clearly correct, and within the current task's scope.
- **Spin off into a follow-up task**: create a new task that captures the finding, then dismiss the comment with the new task ID in the dismissal trail.
  - Apply when the suggestion is real and worth doing, but out of scope for the current task.
  - Procedure (run inline; do NOT call any defer-style helper — the comment text and follow-up task summary live exclusively in the description and dismissal note):
    1. Pick a one-line summary from the comment text. Run `tusk dupes check "<summary>" --json --domain <task domain captured in Step 2>`. Exit code 0 means no duplicate; exit code 1 means a duplicate was found and `matched_task_id` points at it (note it and skip to step 4).
    2. If `FOLLOWUP_TASK_TYPE` (resolved in Step 1) is null, print "Skipped follow-up task — no suitable task_type in config (not 'bug'): <summary>", run `tusk review resolve <comment_id> dismissed`, and continue. Do NOT create the follow-up.
    3. Otherwise insert the follow-up:
       ```bash
       tusk task-insert "<summary>" "<comment text + file path + line range>" \
         --priority Medium \
         --domain <task domain captured in Step 2> \
         --task-type "$FOLLOWUP_TASK_TYPE" \
         --criteria "Address review finding: <summary>"
       ```
       Capture the new `task_id` from the JSON output.
    4. Resolve the comment as dismissed: `tusk review resolve <comment_id> dismissed`. In the rationale you record below, include `Tracked as TASK-<new_id>` (or `Duplicate of TASK-<matched_task_id>` for the dupe path) so the audit trail of "where did this go" survives.
- **Dismiss outright**: run `tusk review resolve <comment_id> dismissed`
  - Apply when the suggestion is low-value, would require significant rework with no clear payoff, or is genuinely a non-issue.

Record every decision (fix, spin off, or dismiss) with a one-line rationale — these will be included in the final summary so the user can review them.

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

1. Start a new review pass and capture the diff size in one call. `tusk review begin` resolves the default branch (`tusk git-default-branch`), computes the `<default>...HEAD` primary range, falls back to the `[TASK-<id>]` commit-range recovery when the feature branch has already been merged and deleted, stamps the captured diff summary onto the new `code_reviews` row internally, and prints a single JSON object with `review_id`, `task_id`, `reviewer`, `range`, `diff_lines`, and `recovered_from_task_commits`. Pass `--pass-num` to bump the pass counter:
   ```bash
   REVIEW_BEGIN_JSON=$(tusk review begin $TASK_ID --pass-num <current_pass + 1>)
   DIFF_LINES=$(printf '%s' "$REVIEW_BEGIN_JSON" | jq -r .diff_lines)
   ```

   If the helper exits non-zero, no diff is recoverable for this pass — surface its stderr verbatim and stop the loop.

2. **Branch on diff size to decide review strategy.**

   **For small or documentation-only diffs (`$DIFF_LINES` below ~200, or only non-code files), when `review.reviewer` is absent from config, or when Tusk is running under a Codex install without an explicit subagent opt-in:** skip agent spawning and perform an inline review. Read the diff yourself, evaluate it against the reviewer focus area, and record the result directly (approve or request-changes + add-comment). After recording the inline decision, skip to step 3.

   To detect the Codex case, read the `install-mode` marker (Claude installs are marked `claude-…`; Codex installs are marked `codex-…`) and check whether the user's `/review-commits` invocation contains an explicit subagent opt-in phrase:

   ```bash
   TUSK_BIN_DIR="$(dirname "$(command -v tusk)")"
   INSTALL_MODE="$(tr -d '[:space:]' < "$TUSK_BIN_DIR/install-mode" 2>/dev/null || echo claude-source)"
   case "$INSTALL_MODE" in codex-*) IS_CODEX=1 ;; *) IS_CODEX=0 ;; esac
   ```

   The user has opted into the agent path only when their invocation explicitly contains a phrase like `use the reviewer agent`, `delegate review`, `spawn the reviewer`, or `agent review`. A bare `/review-commits` (or one with only a task ID argument) is **not** an opt-in. When `IS_CODEX=1` without an opt-in phrase, take the inline path on this re-review pass too.

   **Mixed-mode caveat:** a repo with both `.claude/` and `AGENTS.md` is marked `claude-*` by `install.sh` (install-mode is decided at install time, not at runtime, and Codex does not inject a `CODEX_*` env var into subprocess environments that we could read instead). If this re-review pass is running from a Codex session in such a repo, `IS_CODEX=0` and the agent path will be attempted — under Codex's subagent policy that spawn may fail. If it does, perform a manual inline review on this pass: read the diff yourself, then use `tusk review approve` or `tusk review request-changes` + `tusk review add-comment`, and skip to step 3.

   **For all other diffs:** verify the required agent sandbox permissions are configured before spawning the re-review agent. Run:

   ```bash
   REVIEW_PERM_CHECK=$(tusk review-check-perms) || { echo "Re-review agent aborted: $REVIEW_PERM_CHECK"; exit 1; }
   ```

   On failure the command prints a single `MISSING: …` line and exits 1. When the check fails, surface to the user:
   > Re-review agent aborted: `<captured MISSING: line>`. Create `.claude/settings.json` or add the missing entries manually, or run `tusk upgrade` to apply them, then restart the session.

   Proceed to spawn the re-review agent only if the check prints `OK`. The re-review agent fetches the diff itself — no diff is passed inline. Refresh the cost-attribution anchors before spawning so Step 6's "Apply agent cost" block can correct this pass's row too:

   ```bash
   ORCH_JSONL=$(tusk review-agent-cost --print-orchestrator-jsonl)
   SPAWN_TS=$(date +%s)
   ```

   Both variables shadow the values captured in Step 5.1 — that's intended; each pass writes a fresh `code_reviews` row, and the agent JSONL spawned for this pass is the only one that should attribute to it.

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

Verdict: <APPROVED | CHANGES REMAINING>
```

Counts aggregate across **all** of the task's reviews (including superseded passes) so the block reflects cumulative findings — but the verdict considers only non-superseded reviews, matching `tusk review verdict`. Suggest findings that were spun off into a follow-up task land in the `dismissed` count (the comment is resolved as dismissed with the new task ID in the rationale); the follow-up task itself shows up in the backlog, not in this block.

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
