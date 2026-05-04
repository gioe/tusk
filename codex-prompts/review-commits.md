# Review Commits — Inline Code Review (Codex)

Orchestrates a single code review against the task's git diff (commits on
the current branch vs the base branch). Reads the diff inline, records
findings, fixes must_fix issues, and handles suggest findings
interactively (fix now, spin off into a follow-up task, or dismiss).

> **Conventions:** Run `tusk conventions search <topic>` for project
> rules. Do not restate convention text inline — it drifts from the DB.

> **Sequential execution — no parallel sub-agents.** Codex has no Task
> tool for spawning a background reviewer agent. The review runs **inline
> in the current Codex session**, regardless of diff size. The tradeoff
> vs. the Claude Code variant: the agent-based path runs in an isolated
> sandbox and offloads cost from the orchestrator session; the inline
> path runs in your active session and counts against its token budget.
> Use this prompt the same way regardless of diff size.

> Use `create-task.md` for task creation — handles decomposition,
> deduplication, criteria, and deps. Use `tusk task-insert` only for
> bulk/automated inserts.

## Arguments

Optional: `/review-commits <task_id>` — if omitted, task ID is inferred
from the current branch name.

---

## Step 0: Start Cost Tracking

First, resolve the task ID. Use the argument if one was passed, otherwise
parse it from the current branch:

```bash
tusk branch-parse
```

Returns `{"task_id": N}` on success. If it exits 1 (branch doesn't match
pattern) and no argument was passed, ask the user to provide a task ID
before continuing. Store the resolved ID as `TASK_ID`.

Then record the start of this review run so cost can be captured at the
end:

```bash
tusk skill-run start review-commits --task-id $TASK_ID
```

This prints `{"run_id": N, "started_at": "...", "task_id": N}`. Capture
`run_id` — you will need it in Step 11.

> **Early-exit cleanup:** If any validity/mode check below causes the
> prompt to stop before Step 11, first call `tusk skill-run cancel
> <run_id>` to close the open row, then stop. Otherwise the row lingers
> as `(open)` in `tusk skill-run list` forever.

## Step 1: Read Config and Check Mode

```bash
tusk config
```

Parse the returned JSON. Extract:
- `review.mode` — if `"disabled"`, run
  `tusk skill-run cancel <run_id>`, print "Review mode is disabled in
  config (review.mode = disabled). Enable it in tusk/config.json to use
  /review-commits." and **stop**.
- `review.max_passes` — maximum fix-and-re-review cycles (default: 2)
- `review.reviewer` — a single reviewer object with `name` and
  `description` fields, or absent. The Codex inline path uses
  `review.reviewer.description` as the focus area but never spawns a
  separate agent.
- `review_categories` — valid comment categories (typically
  `["must_fix", "suggest"]`)
- `review_severities` — valid severity levels (typically
  `["critical", "major", "minor"]`)
- `task_types` — list of valid task type strings. Resolve the best type
  for follow-up tasks created from `suggest` findings now: prefer
  `"refactor"`, then `"chore"`, then the first entry that is not
  `"bug"`. Store as `FOLLOWUP_TASK_TYPE`. If the list is empty or every
  entry is `"bug"`, set `FOLLOWUP_TASK_TYPE = null`.

## Step 2: Verify Task and Capture Domain

`TASK_ID` was resolved in Step 0. Verify the task exists and capture its
domain:

```bash
tusk -header -column "SELECT id, summary, status, domain FROM tasks WHERE id = $TASK_ID"
```

If no row is returned, run `tusk skill-run cancel <run_id>` to close the
open row, then abort: "Task `$TASK_ID` not found."

Store the task's `domain` value — Step 7 uses it when dupe-checking and
creating follow-up tasks from `suggest` findings.

## Step 3: Get the Git Diff

Compute the diff range in one call — the helper handles the
default-branch resolution (`tusk git-default-branch`), the
`<default>...HEAD` primary range, and the `[TASK-<id>]` commit-range
recovery fallback used when the feature branch has already been merged
and deleted:

```bash
DIFF_RANGE_JSON=$(tusk review-diff-range $TASK_ID)
```

On success the helper prints a single JSON object with four keys
(`range`, `diff_lines`, `summary`, `recovered_from_task_commits`) and
exits 0. Capture:

```bash
DIFF_RANGE=$(printf '%s' "$DIFF_RANGE_JSON" | jq -r .range)
DIFF_LINES=$(printf '%s' "$DIFF_RANGE_JSON" | jq -r .diff_lines)
DIFF_SUMMARY=$(printf '%s' "$DIFF_RANGE_JSON" | jq -r .summary)
```

> Use `printf '%s'` rather than `echo "$VAR"`. In zsh — and in bash with `xpg_echo` enabled — `echo` interprets the literal `\n` escape sequences inside the captured JSON as real newlines, breaking jq with `Invalid string: control characters from U+0000 through U+001F must be escaped` and silently leaving `$DIFF_SUMMARY` empty.

If the helper exits non-zero, no diff is recoverable — either no
`[TASK-<id>]` commits were found in recent history, or the recovered
range is still empty. Run `tusk skill-run cancel <run_id>` and stop,
surfacing the helper's stderr verbatim.

Use `$DIFF_RANGE` for any subsequent `git diff` call in this prompt, and
pass `$DIFF_SUMMARY` to `tusk review start` (Step 4).

## Step 4: Start the Review

Start a review record for the task. This creates one `code_reviews` row
using the configured reviewer (or unassigned if `review.reviewer` is
absent):

```bash
tusk review start $TASK_ID --diff-summary "$DIFF_SUMMARY"
```

The command prints a single line, for example:

```
Started review #12 for task #42 (reviewer: general): Fix login bug
```

Capture the printed `review_id`.

## Step 5: Inline Review

Run the review inline — Codex has no background agent path.

### Step 5.1: Fetch the Diff

```bash
git diff "$DIFF_RANGE"
```

If the output is empty (e.g. the commits were already merged and the
recovery in Step 3 still produced an empty range), run `tusk skill-run
cancel <run_id>`, surface the empty-diff message, and stop.

### Step 5.2: Analyze for Issues

For each issue: category, severity, file path, line number, clear
actionable description. Check all seven dimensions:

1. **Correctness** — logic errors, edge cases, race conditions,
   contradicts acceptance criteria
2. **Security** — injection, auth bypass, data exposure, input
   validation, secrets
3. **Readability** — unclear naming, functions doing too much, dead
   code, what-not-why comments
4. **Design** — unnecessary coupling, DRY violations, premature
   abstraction, pattern inconsistency
5. **Tests** — missing coverage, wrong assertions, untested failure
   paths
6. **Performance** — N+1 queries, expensive ops in hot paths,
   unjustified new dependencies
7. **Operational** — unsafe migrations, insufficient logging, missing
   rollback plan

**Wrappers and delegation layers** (context providers, decorators,
middleware, DI containers): do not flag as unused based on shallow
traversal. Consumer usage can exist arbitrarily deep. Grep *all* files
reachable from the wrapper's consumers for the exposed interface before
flagging. If the search is incomplete or inconclusive, downgrade to
`suggest`.

**`tusk "<raw SQL>"` is a valid invocation pattern, not wrong syntax.**
The `bin/tusk` dispatcher routes every unrecognized subcommand to
`cmd_query` — its raw-SQL passthrough — so `tusk "SELECT ..."`,
`tusk "INSERT ..."`, and `tusk "UPDATE ..."` all execute the given SQL
against the project's `tasks.db`. Do **not** flag `tusk "<SQL string>"`
as "unknown command" or "wrong syntax" in a review.

### Step 5.3: Verify Final State Before Flagging must_fix

Before recording any `must_fix`, confirm the pattern exists in the final
state — not just in a `-` diff line:

```bash
git show HEAD:<file_path> | grep -n "<pattern>"
```

- Present → proceed to flag.
- Absent → check whether the code moved:
  ```bash
  git diff "$DIFF_RANGE" | grep "^+" | grep -F "<pattern>"
  ```
  If it appears in `+` lines of another file (identify from the
  `+++ b/<file>` header), confirm with `git show HEAD:<destination>` and
  update the finding's file/line. Otherwise discard — it was truly
  removed.

Required for `must_fix` only. `suggest` doesn't need final-state
verification.

### Step 5.4: Verification Constraints

**Never run the full test suite during review.** Limit verification to
`git show HEAD:<file> | grep <pattern>` and similarly cheap commands.
For collection-error checks only: `pytest --collect-only -q`
(sub-second). If you can't verify a finding with `git show` + `grep`,
downgrade from `must_fix` to `suggest`.

### Step 5.5: Record Findings

For each issue:

```bash
tusk review add-comment $REVIEW_ID "<description and how to fix>" \
  --file "<file path>" --line-start <line> \
  --category <must_fix|suggest> --severity <critical|major|minor>
```

Omit `--file` and `--line-start` for general comments.

### Step 5.6: Submit the Verdict

Always pass `--model <your_model_id>` — the canonical model ID matching
the format in `task_sessions.model` (e.g. `claude-opus-4-7`,
`claude-sonnet-4-6`, `claude-haiku-4-5`, or your Codex runner's
equivalent ID). Strip any suffixes (e.g. `[1m]`) so the value joins
cleanly against other model-tagged tables.

- Any must_fix findings:
  ```bash
  tusk review request-changes $REVIEW_ID --model <your_model_id>
  ```
- No must_fix findings:
  ```bash
  tusk review approve $REVIEW_ID --model <your_model_id>
  ```

## Step 6: (Reserved)

(Step numbering follows the original `/review-commits` flow; in the
Codex port the agent-monitoring step is folded into Step 5.)

## Step 7: Process Findings

After recording the verdict, fetch the full review results:

```bash
tusk review list $TASK_ID
```

Gather all open (unresolved) comments from the review. Before processing
any comments, initialize a list of files you touch during review fixes
— Step 9 uses this list to stage only the files you actually modified:

```bash
REVIEW_FIX_FILES=()
```

Group the open comments by category:

### must_fix comments

For each open `must_fix` comment:
1. Read the comment details (file path, line numbers, comment text,
   severity).
2. Implement the fix directly in the codebase.
3. Record every file you modified while addressing this comment —
   usually the comment's own `file_path`, plus any additional files the
   fix required (new tests, helper extraction, etc.):
   ```bash
   REVIEW_FIX_FILES+=("<file_path>")
   ```
4. After fixing, mark the comment resolved:
   ```bash
   tusk review resolve <comment_id> fixed
   ```

### suggest comments

These are optional improvements. For each `suggest` comment, **decide
autonomously** between three branches — do not ask the user:

- **Fix**: implement the suggestion, append every file you modified to
  `REVIEW_FIX_FILES`, then run
  `tusk review resolve <comment_id> fixed`.
  Apply when the fix is small, clearly correct, and within the current
  task's scope.
- **Spin off into a follow-up task**: create a new task that captures
  the finding, then dismiss the comment with the new task ID in the
  dismissal trail. Apply when the suggestion is real and worth doing
  but out of scope for the current task.
  Procedure (run inline; do NOT call any defer-style helper — the
  comment text and follow-up task summary live exclusively in the
  description and dismissal note):
    1. Pick a one-line summary from the comment text. Run
       `tusk dupes check "<summary>" --json --domain <task domain captured in Step 2>`.
       Exit code 0 means no duplicate; exit code 1 means a duplicate
       was found and `matched_task_id` points at it (note it and skip
       to step 4).
    2. If `FOLLOWUP_TASK_TYPE` (resolved in Step 1) is null, print
       "Skipped follow-up task — no suitable task_type in config (not
       'bug'): <summary>", run
       `tusk review resolve <comment_id> dismissed`, and continue.
       Do NOT create the follow-up.
    3. Otherwise insert the follow-up:
       ```bash
       tusk task-insert "<summary>" "<comment text + file path + line range>" \
         --priority Medium \
         --domain <task domain captured in Step 2> \
         --task-type "$FOLLOWUP_TASK_TYPE" \
         --criteria "Address review finding: <summary>"
       ```
       Capture the new `task_id` from the JSON output.
    4. Resolve the comment as dismissed:
       `tusk review resolve <comment_id> dismissed`. In the rationale
       you record below, include `Tracked as TASK-<new_id>` (or
       `Duplicate of TASK-<matched_task_id>` for the dupe path) so the
       audit trail of "where did this go" survives.
- **Dismiss outright**: run
  `tusk review resolve <comment_id> dismissed`.
  Apply when the suggestion is low-value, would require significant
  rework with no clear payoff, or is genuinely a non-issue.

Record every decision (fix, spin off, or dismiss) with a one-line
rationale — these will be included in the final summary so the user can
review them.

After processing all findings, check the current verdict:

```bash
tusk review-verdict $TASK_ID
```

This returns `{"verdict": "APPROVED|CHANGES_REMAINING", "open_must_fix": N}`.
If `verdict` is `APPROVED` and no `must_fix` changes were made, skip
Step 8 and proceed directly to Step 9.

## Step 8: Re-review Loop (if there were must_fix changes)

If any `must_fix` comments were fixed in Step 7, re-run the review to
verify the fixes are correct. Check pass status before starting:

```bash
tusk review-pass-status $TASK_ID
```

This returns
`{"current_pass": N, "max_passes": N, "can_retry": bool, "open_must_fix": N}`.

If `can_retry` is false (either no open `must_fix` items, or
`current_pass >= max_passes`), do not enter the loop. If
`open_must_fix > 0` and `can_retry` is false, **escalate to the user**:

> Max review passes (`max_passes`) reached. The following must_fix items
> remain unresolved:
> <list each open must_fix comment>
>
> Please resolve these manually before continuing.

Otherwise, loop while `can_retry` is true:

1. Start a new review pass:
   ```bash
   tusk review start $TASK_ID --pass-num <current_pass + 1> --diff-summary "Re-review pass <n>"
   ```

2. Recompute the diff range:
   ```bash
   DIFF_RANGE=$(tusk review-diff-range $TASK_ID | jq -r .range)
   ```

3. Run the inline review again — repeat Step 5 (fetch diff, analyze,
   verify final state, verification constraints, record findings,
   submit verdict). Then process the new findings via Step 7.

4. Re-check pass status to determine whether to continue:
   ```bash
   tusk review-pass-status $TASK_ID
   ```
   If `can_retry` is still true and `open_must_fix > 0`, repeat from
   step 1. If `can_retry` is false and `open_must_fix > 0`, escalate to
   the user with the same message as above.

If `tusk review-verdict $TASK_ID` returns `"verdict": "APPROVED"` and no
new blocking findings were raised, proceed to Step 9.

## Step 9: Commit Review Fixes

Before summarizing, ensure all changes made during review are committed.
Check for any uncommitted modifications:

```bash
git diff --stat
git diff --cached --stat
```

If both commands show no output, the working tree is clean — skip this
step.

Otherwise, commit **only** the files you tracked in `REVIEW_FIX_FILES`
during Steps 7 and 8. **Never use `git add -A` or `git add .`** —
those stage every dirty or untracked file in the working tree,
including unrelated changes from other sessions.

First, deduplicate the tracked list and reconcile it against the actual
diff **before** staging or committing:

```bash
# Deduplicate the tracked file list
REVIEW_FIX_FILES=($(printf '%s\n' "${REVIEW_FIX_FILES[@]}" | sort -u))

# Abort if no files were tracked but a diff exists — investigate manually
if [ ${#REVIEW_FIX_FILES[@]} -eq 0 ]; then
  echo "ERROR: uncommitted changes exist but REVIEW_FIX_FILES is empty. Review the diff above and stage files explicitly by name." >&2
  exit 1
fi
```

Re-run `git diff --stat` and `git diff --cached --stat` and compare the
listed paths to `REVIEW_FIX_FILES`. If any path you *did* modify during
review is missing from the array, append it explicitly by name (never
fall back to `git add -A`):

```bash
REVIEW_FIX_FILES+=("<path-you-modified>")
```

Conversely, any remaining unstaged paths that are **not** in
`REVIEW_FIX_FILES` must be scratch work from other sessions — leave
them alone.

Once the list is reconciled, stage, commit, and push in a single pass:

```bash
git add -- "${REVIEW_FIX_FILES[@]}"
git commit -m "[TASK-$TASK_ID] Apply review fixes"
git push --set-upstream origin HEAD
```

`--set-upstream origin HEAD` is required on the **first** push of a
brand-new feature branch when `push.autoSetupRemote` is not set in the
user's git config. The flag is idempotent on subsequent pushes.

## Step 10: Final Summary

Render the final summary block in one call — the helper reads all
counts from `code_reviews` / `review_comments`, computes the verdict the
same way as `tusk review verdict`, and maps `APPROVED` /
`CHANGES_REMAINING` to the display label:

```bash
tusk review-final-summary $REVIEW_ID
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

## Step 11: Finish Cost Tracking

Record cost for this review run:

- `must_fix_count` — the `open_must_fix` value from
  `tusk review-verdict` in Step 10.
- `passes` — the final pass number printed in Step 10's summary block.
- `diff_lines` — the `DIFF_LINES` value captured in Step 3.

```bash
tusk skill-run finish $RUN_ID --metadata '{"must_fix_count":<M>,"passes":<P>,"diff_lines":<D>}'
```

To view cost history across all review-commits runs:

```bash
tusk skill-run list review-commits
```
