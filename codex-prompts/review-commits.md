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

### Resolve the Tusk wrapper for this checkout

Before the first Tusk command, resolve an executable from the active checkout.
This step overrides any outer Codex wrapper instruction that names a fixed
`.claude/bin/tusk` path: that generated directory may be absent from a task
worktree. Prefer checkout-local wrappers so review commands exercise the code
and Git state in the workspace being reviewed.

```bash
REVIEW_REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
REVIEW_TUSK_BIN=""
for candidate in \
  "$REVIEW_REPO_ROOT/bin/tusk" \
  "$REVIEW_REPO_ROOT/tusk/bin/tusk" \
  "$REVIEW_REPO_ROOT/.claude/bin/tusk"
do
  if [ -x "$candidate" ]; then
    REVIEW_TUSK_BIN="$candidate"
    break
  fi
done
if [ -z "$REVIEW_TUSK_BIN" ]; then
  REVIEW_TUSK_BIN=$(command -v tusk || true)
fi
if [ -z "$REVIEW_TUSK_BIN" ]; then
  echo "Review aborted: no executable Tusk wrapper found for this checkout." >&2
  exit 1
fi

# Command execution follows the checkout-local wrapper above, but install mode
# describes the installed agent surface that invoked this workflow. Follow its
# complete symlink chain before looking for the sibling marker; machine-level
# wrappers commonly live in ~/.local/bin without a marker of their own.
INSTALL_MODE_SOURCE=$(command -v tusk || true)
if [ -z "$INSTALL_MODE_SOURCE" ]; then
  INSTALL_MODE_SOURCE="$REVIEW_TUSK_BIN"
fi
while [ -L "$INSTALL_MODE_SOURCE" ]; do
  INSTALL_MODE_SOURCE_DIR=$(cd -P "$(dirname "$INSTALL_MODE_SOURCE")" && pwd)
  INSTALL_MODE_SOURCE_TARGET=$(readlink "$INSTALL_MODE_SOURCE")
  case "$INSTALL_MODE_SOURCE_TARGET" in
    /*) INSTALL_MODE_SOURCE="$INSTALL_MODE_SOURCE_TARGET" ;;
    *) INSTALL_MODE_SOURCE="$INSTALL_MODE_SOURCE_DIR/$INSTALL_MODE_SOURCE_TARGET" ;;
  esac
done
INSTALL_MODE_SOURCE_DIR=$(cd -P "$(dirname "$INSTALL_MODE_SOURCE")" && pwd)
if [ -f "$INSTALL_MODE_SOURCE_DIR/install-mode" ]; then
  INSTALL_MODE=$(tr -d '[:space:]' < "$INSTALL_MODE_SOURCE_DIR/install-mode")
else
  INSTALL_MODE=claude-source
fi
case "$INSTALL_MODE" in codex|codex-*) IS_CODEX=1 ;; *) IS_CODEX=0 ;; esac
printf '%s\n' "$REVIEW_TUSK_BIN"
```

Capture the printed absolute path as `REVIEW_TUSK_BIN`, and capture the resolved
`INSTALL_MODE` and `IS_CODEX` values, in orchestrator state. The Codex port
always reviews inline, but retaining the same install-mode contract keeps its
wrapper guidance aligned with the canonical workflow.
Every `tusk ...` example below means “invoke that exact resolved path with these
arguments.” Tool calls may run in separate shells, so do not assume the shell
variables or a shell function persist between calls. Do not continue using a
fixed wrapper path supplied by the invocation wrapper for Tusk commands after
this resolution.

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

On success the helper prints a single JSON object with five keys
(`range`, `diff_lines`, `diff_lines_meaningful`, `summary`,
`recovered_from_task_commits`) and exits 0. Capture:

```bash
DIFF_RANGE=$(printf '%s' "$DIFF_RANGE_JSON" | jq -r .range)
DIFF_LINES=$(printf '%s' "$DIFF_RANGE_JSON" | jq -r .diff_lines)
# Lockfile-subtracted line count (issue #761); use this when reasoning
# about review effort rather than the raw diff_lines.
DIFF_LINES_MEANINGFUL=$(printf '%s' "$DIFF_RANGE_JSON" | jq -r '.diff_lines_meaningful // .diff_lines')
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

### Step 5.2.5: Ground Every Finding in the Real Diff

**Every `file_path` you pass to `tusk review add-comment` MUST appear
verbatim in the diff's `+++ b/<file>` headers.** Run the diff yourself
(Step 5.1) and read the `+++ b/<path>` lines — those define the universe
of files you may name. If a finding describes behavior at a path not in
that list, the path does not exist on this branch — discard the finding
rather than recording it.

Step 7's `tusk review validate-comments $REVIEW_ID` call enforces this
automatically by re-deriving the diff range and **auto-dismissing every
comment whose `file_path` is not in `git diff --name-only`** (issue #783).
Every fabricated comment becomes a visible dismissal the user reads, so
pattern-matching plausible-sounding "adjacent" files from the task
description is not silently swallowed.

**General comments (`file_path` omitted, no `--file`) MUST quote a
specific diff line in the description.** Use a fenced `+` or `-` line
from the diff so the human reviewer can map the comment back to a
concrete change. A general comment with no diff anchor is a code-smell —
split it into per-file findings, drop it, or rephrase as a `suggest`
with the anchor line included.

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

Add `--spec-gap-type <type>` when a finding reveals why it exists:
`implementation_failure`, `ambiguous_spec`, `missing_criterion`,
`missing_verification`, or `design_discovery`. Use
`implementation_failure` when the task spec was adequate and the code
missed it; use the other values when the task itself needs stronger
handoff context, criteria, verification, or decomposition.

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

After recording the verdict, validate that every pending comment's
`file_path` actually appears in the diff (issue #783 fabrication guard),
and that general comments (null `file_path`) cite only in-diff paths if
they cite paths at all unless those cited paths exist in the repo (issue
#912 fabrication guard plus issue #985 out-of-diff-real routing).
`tusk review validate-comments` re-derives the diff range and auto-dismisses
any pending comment whose `file_path` is missing from `git diff --name-only`.
For general comments it body-scans for file-path-shaped tokens and dismisses
the comment when every cited path is out-of-diff and none of those paths
resolve to real repo files. If at least one cited out-of-diff path exists,
the comment is preserved and returned under `out_of_diff_real` for follow-up
task routing. Stale-line comments whose referenced symbol exists elsewhere in
the same in-diff file are preserved under `flagged_symbol_mismatch`: the
reviewer may have found a real issue on a moved symbol, but the line anchor is
unreliable. In the Codex inline path the orchestrator IS the reviewer, so
fabrication is rare — but the validation also catches stale `file_path` values
left over from earlier renames and stale line anchors, so it still earns its
keep on the inline path:

```bash
VALIDATION_JSON=$(tusk review validate-comments $REVIEW_ID)
DISMISSED_COUNT=$(printf '%s' "$VALIDATION_JSON" | jq '(.dismissed | length) + (.dismissed_general | length)')
OUT_OF_DIFF_REAL_COUNT=$(printf '%s' "$VALIDATION_JSON" | jq '(.out_of_diff_real // [] | length)')
FLAGGED_SYMBOL_MISMATCH_COUNT=$(printf '%s' "$VALIDATION_JSON" | jq '(.flagged_symbol_mismatch // [] | length)')
```

If `$DISMISSED_COUNT > 0`, surface both `dismissed` (file_path-driven) and
`dismissed_general` (body-scan-driven) entries verbatim so the user can see
what was dropped. If `$OUT_OF_DIFF_REAL_COUNT > 0`, surface those entries
separately as scope-adjacent findings: the cited files exist in the repo but
are not part of this diff. Do not fix those files in the current review unless
the task scope already allows it; create or recommend a focused follow-up task
when the substance is valid. If `$FLAGGED_SYMBOL_MISMATCH_COUNT > 0`, surface
those entries separately as stale-line symbol findings. Re-review the cited
symbol in the current diff before acting: fix valid in-scope findings at the
symbol's current location, create or recommend a focused follow-up for valid
out-of-scope findings, and manually dismiss unactionable stale anchors with
that rationale. Do not ignore these preserved entries. Then fetch the full
review results:

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
   If the fix exposed a spec authoring gap, carry the classification:
   `tusk review resolve <comment_id> fixed --spec-gap-type missing_criterion`
   or `--spec-gap-type missing_verification`.

### suggest comments

These are optional improvements. For each `suggest` comment, **decide
autonomously** between four branches — do not ask the user:

Before choosing a branch, check whether the comment is really a spec
gap. If it says the task lacked an acceptance criterion, record
`--spec-gap-type missing_criterion` and either add the missing
criterion now with `tusk criteria add $TASK_ID "<criterion>"` when it
belongs to the current task, preserve the learning as
`tusk context add $TASK_ID --source review --type decision|assumption|risk|question|memory ...`,
or spin it into a follow-up task. If it says the task lacked proof,
record `--spec-gap-type missing_verification` and add a typed
verification criterion when possible, otherwise preserve context or
create a follow-up. Use `ambiguous_spec` for unclear intent and
`design_discovery` when review surfaced a new design decision that
should be durable.

- **Fix**: implement the suggestion, append every file you modified to
  `REVIEW_FIX_FILES`, then run
  `tusk review resolve <comment_id> fixed`.
  Apply when the fix is small, clearly correct, and within the current
  task's scope.
- **Preserve as a context atom**: create a task context atom, then
  dismiss the comment with the context item ID in the dismissal trail.
  Apply when the finding is useful future context but does not require
  shippable work.
  - Use `tusk context add $TASK_ID --source review --type decision --content "<durable design decision>"`
    when the review resolves toward an intentional design choice.
  - Use `tusk context add $TASK_ID --source review --type assumption --content "<assumption future agents should preserve>"`
    when the dismissal depends on an assumption that may matter later.
  - Use `tusk context add $TASK_ID --source review --type risk --content "<future risk and trigger condition>"`
    when the finding names scoped risk that is real but not immediate
    work.
  - Use `tusk context add $TASK_ID --source review --type question --content "<open question and why it is not blocking now>"`
    when the finding exposes an open question that should survive
    handoff.
  - Use `tusk context add $TASK_ID --source review --type memory --content "<durable implementation note>"`
    for other durable facts that would help a future run.
  - Do not write directly to `task_context_items`; use the first-class
    context CLI.
  - After creating the context atom, dismiss the comment with
    `tusk review resolve <comment_id> dismissed --note "<rationale>; preserved as <type> context atom #<context_item_id>"`.
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
  If the dismissal rationale contains a durable design reason,
  assumption, future risk, open question, or implementation memory,
  first record the smallest useful context atom with
  `tusk context add $TASK_ID --source review --type decision|assumption|risk|question|memory --content "<content>"`,
  then include the context item ID in the dismissal note.

Record every decision (fix, preserve as context atom, spin off, or
dismiss) with a one-line rationale — these will be included in the final
summary so the user can review them.

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
`{"current_pass": N, "max_passes": N, "can_retry": bool, "open_must_fix": N, "fixed_must_fix": N}`.
The finding counts describe only the latest non-superseded review: a fixed
`must_fix` makes that pass eligible for one verification pass, while a clean
verification pass does not retrigger because findings from earlier passes are
ignored.

If `can_retry` is false, do not enter the loop. A clean latest pass
(`open_must_fix == 0` and `fixed_must_fix == 0`) needs no further verification.
If `open_must_fix > 0` and `can_retry` is false because
`current_pass >= max_passes`, **escalate to the user**:

> Max review passes (`max_passes`) reached. The following must_fix items
> remain unresolved:
> <list each open must_fix comment>
>
> Please resolve these manually before continuing.

Otherwise, loop while `can_retry` is true:

1. **Commit the fixes that the next pass must inspect.** The re-review diff
   ends at committed `HEAD`; do not start another pass with review fixes only
   in the working tree. Deduplicate the tracked paths, abort if none were
   recorded, then stage and commit only `REVIEW_FIX_FILES`:

   ```bash
   REVIEW_FIX_FILES=($(printf '%s\n' "${REVIEW_FIX_FILES[@]}" | sort -u))
   if [ ${#REVIEW_FIX_FILES[@]} -eq 0 ]; then
     echo "ERROR: re-review requested but REVIEW_FIX_FILES is empty. Record the review-fix paths before starting another pass." >&2
     exit 1
   fi

   git diff --stat
   git diff --cached --stat
   git add -- "${REVIEW_FIX_FILES[@]}"
   git commit -m "[TASK-$TASK_ID] Apply review fixes" -- "${REVIEW_FIX_FILES[@]}"
   git push --set-upstream origin HEAD
   REVIEW_FIX_FILES=()
   ```

   The pathspec on `git commit` prevents unrelated paths that were already
   staged from leaking into the fix commit. Leave all other tracked or
   untracked working-tree changes untouched. If staging, committing, or
   pushing fails, stop before creating the next review row.

2. Start a new review pass:
   ```bash
   tusk review start $TASK_ID --pass-num <current_pass + 1> --diff-summary "Re-review pass <n>"
   ```

3. Recompute the diff range:
   ```bash
   DIFF_RANGE=$(tusk review-diff-range $TASK_ID | jq -r .range)
   ```

4. Run the inline review again — repeat Step 5 (fetch diff, analyze,
   verify final state, verification constraints, record findings,
   submit verdict). Then process the new findings via Step 7.

5. Re-check pass status to determine whether to continue:
   ```bash
   tusk review-pass-status $TASK_ID
   ```
   If `can_retry` is still true, repeat from step 1. This includes the
   fixed-finding state where `open_must_fix == 0` and
   `fixed_must_fix > 0`. If `can_retry` is false and
   `open_must_fix > 0`, escalate to the user with the same message as
   above.

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
git commit -m "[TASK-$TASK_ID] Apply review fixes" -- "${REVIEW_FIX_FILES[@]}"
git push --set-upstream origin HEAD
REVIEW_FIX_FILES=()
```

The path-limited commit is a final safeguard against unrelated paths that
were already staged before review. It commits only the files recorded in
`REVIEW_FIX_FILES`; all other tracked or untracked working-tree changes remain
untouched.

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
context:   <review_source_count> atoms preserved from review

Verdict: <APPROVED | CHANGES REMAINING>
```

The context count comes from `task_context_items` rows for this task
with `source='review'`; it is the audit cue for review decisions
preserved outside the backlog.

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
