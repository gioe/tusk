# Tusk — Conditional Recovery

Read only the section selected by the core workflow trigger. These instructions
retain that workflow’s gates and route back to its numbered steps.

<a id="deliverable-check"></a>

## Deliverable-check recommendations

**Deliverable check:** If `deliverable_check_needed` from Step 1 is
`true`, run:
```bash
tusk check-deliverables <id>
```
This command checks all branches for commits referencing the task
and, if none are found, scans the task description and criteria for
referenced file paths and tests whether they exist on disk. Act on
the `recommendation` field:
- **`"commits_found"`** — `[TASK-<id>]` commits exist on a
  non-default branch (typically a stale feature branch from a prior
  session). Switch to it or cherry-pick the relevant commits before
  proceeding to Explore.
- **`"merged_not_closed"`** — `[TASK-<id>]` commits already exist on
  the default branch (orphaned-task case: work was merged without
  being finalized through `tusk merge`). Skip implementation
  entirely. Mark all criteria done with `--skip-verify`, then jump
  straight to Step 12 to close out the session.
- **`"mark_done"`** — no commits, but deliverable files listed in
  `files` already exist on disk AND the verification-spec gate
  passed (all runnable positive specs pass, or no runnable spec
  exists). Negative/absence specs never count as positive
  convergence evidence; inspect the positive/negative spec counts
  alongside the legacy total counts.
  Mark all criteria done with `--skip-verify` and proceed directly
  to Step 9 (commit + merge) without reimplementing.
- **`"criteria_complete_no_commits"`** — every non-deferred
  acceptance criterion is already marked `is_completed=1`, but
  there are no `[TASK-<id>]` commits anywhere AND no deliverable
  files on disk. This is a **salvage / converged-work /
  speculative-mark** signal (issue #578, original incident
  TASK-1714): a prior session marked criteria done without
  producing any committed deliverable. Common causes: (1)
  lost-work — a prior agent did real work but couldn't commit
  cleanly (dirty worktree, branch protection, bundled unrelated
  changes on a salvage branch); (2) convergent-evolution — separate
  tasks effectively achieved the goal, so no fresh commits are
  needed for THIS task; (3) speculative pre-marking — criteria
  were marked done at the start of a prior session without backing
  code. **Do NOT silently proceed as `implement_fresh`.** Instead:
  (a) read the task's progress notes via `tusk task-get <id>` and
  inspect any `next_steps` references; (b)
  `git branch -a | grep TASK-<id>` for stale branches and inspect
  their diff against the default branch (`git log <branch>..origin/<default>`
  and `git show <sha>`) to determine whether the work is obsolete
  vs. still relevant; (c) surface the options to the user —
  **re-implement** (proceed with Explore → Implement as if
  `implement_fresh`), **accept-as-converged** (close via
  `tusk abandon <id> --reason completed --note "<rationale referencing the converging task or commits>"`),
  or **abandon** (close via
  `tusk abandon <id> --reason wont_do --note "..."`). Do not pick
  the path unilaterally.
- **`"implement_fresh"`** — no commits and either no deliverable
  files were found, or files exist but every incomplete code/file
  verification spec still fails (issue #1068: the deliverable is
  an EDIT to an existing referenced file, so file existence is
  noise — `files_found` stays `true` and `verifiable_spec_count`
  > 0 with `passing_spec_count` = 0 records the downgrade).
  Proceed normally and implement from scratch.

<a id="stalled-exploration"></a>

## Stalled exploration

**Bounded recovery for a stalled exploration subagent.** Do not wait
indefinitely for mandatory exploration. After a reasonable interval with no
material progress, inspect the task worktree and the subagent's latest
report. Material progress includes a worktree diff, completed command or test
output, or a substantive report of finished work or a concrete blocker; an
active/running status alone is not progress. On the first no-progress check,
send one focused nudge that restates or narrows the exploration assignment.
If the next progress check still shows no material progress, interrupt the
subagent. Then either delegate one narrower replacement exploration
assignment or complete the required exploration locally. A replacement gets
the same single-nudge budget; if it also stalls, interrupt it and fall back
locally rather than spawning another replacement. On local fallback, complete
the same exploration checklist, report findings before writing any code, and
surface `Exploration routing: local fallback — <stalled evidence; local
findings>`. Do not interrupt an actively producing command or test before it
finishes or reaches its own timeout.

<a id="stalled-implementation"></a>

## Stalled implementation

**Bounded recovery for a stalled implementation subagent.** Do not wait
indefinitely for delegated implementation. After a reasonable interval with
no material progress, inspect the task worktree and the subagent's latest
report. Material progress includes a worktree diff, completed command or test
output, or a substantive report of finished work or a concrete blocker; an
active/running status alone is not progress. On the first no-progress check,
send one focused nudge that restates or narrows the assignment. If the next
progress check still shows no material progress, interrupt the subagent. Then
either delegate a narrower replacement assignment or continue locally. A
local fallback is allowed for any task size only after this bounded recovery
sequence; surface the route change and evidence using `Implementation routing:
local fallback — <stalled evidence; reason for continuing locally>`. Do not
interrupt an actively producing command or test before it finishes or reaches
its own timeout.

<a id="commit-recovery"></a>

## Commit recovery

**If `tusk commit` exits 9 (concurrent commit active),** another
invocation holds the operation lock for the same worktree and this
process did not run `git commit`. Wait for the active invocation to
finish and inspect its `TUSK_COMMIT_RESULT`. Retry only when that
result shows the requested commit did not land. If the result is
unavailable, inspect HEAD with `git log -1 --format='%H %s'`, the
selected paths with `git status --short -- "<file1>" ["<file2>" ...]`,
and criterion bindings with `tusk criteria list <id>` before deciding.
If the requested TASK commit and intended criterion bindings landed
and the selected requested changes are clean, do not reissue
`tusk commit`; if the evidence is inconsistent, investigate instead
of retrying blindly. Do not interpret exit 9 as a Git failure or mark
criteria directly from the losing invocation.

**Raw Git fallback attribution:** Replace the attribution placeholder in the examples below with the executing agent's known name and email in Git's standard `Name <email>` form; do not copy a historical model identity or infer it from this prompt's filename. If the identity is unavailable, omit the trailer and report the limitation rather than guessing. This guidance applies to raw Git fallbacks; the normal `tusk commit` command supplies its own trailer.

**If `tusk commit` fails with `pathspec did not match any files`**
(exit code 3, git-add error), first check whether the file was
already committed in a prior `tusk commit` for this task, or
whether it was removed via `git rm` (which stages the deletion).
In either case, `git add && git commit` would also fail — just mark
the remaining criteria done directly:
```bash
tusk criteria done <cid> --skip-verify
```
If the error is a genuine pathspec mismatch, always pass file
paths relative to the repo root. If the error persists, fall back
to:
```bash
git add "<file1>" ["<file2>" ...] && git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: <executing agent name and email>"
```
Then mark criteria done with `tusk criteria done <cid> --skip-verify`.

**If `tusk commit` fails with `pathspec '…' is beyond a symbolic
link`** (exit code 3), the path lives under a symlinked directory
that `git add` refuses to traverse. Retry with the real source
path. More generally: if `ls -la` on any parent directory shows it
is a symlink, use the link's target path instead.

**If a pre-commit auto-formatter rewrites a staged file in-place**,
`tusk commit` detects the index/working-tree divergence, re-stages
the reformatted content, and retries the commit exactly once. If
the retry also fails (the formatter produces unstable output on
every run), bypass hooks with:
```bash
tusk commit <task_id> "<message>" "<file>" --skip-verify
```

**If the commit removes a file from git tracking** (i.e., the
staged change is a `git rm --cached` deletion, not a file
modification), do NOT use `tusk commit` — it retries gitignored
paths with `git add -f`, which re-adds the file and defeats the
deletion. Use `git commit` directly:
```bash
git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: <executing agent name and email>"
```
Then mark criteria done with `tusk criteria done <cid> --skip-verify`.

**If `tusk commit` hard-fails because tests fail** (exit code 2 —
`test_command` is set and returned non-zero), **first verify the
failure is not pre-existing** before entering the diagnosis loop:

**Pre-existing failure check** — always pass `--flake-retries 2`
on this post-gate-failure precheck so flake detection actually
fires (without it `flaky_suspect` is never emitted and an
intermittent test reads as a real regression):
```bash
tusk test-precheck --flake-retries 2
```
Or pass an explicit command when the config-resolved one isn't
what you want to check against:
```bash
tusk test-precheck --command "<test_command>" --flake-retries 2
```
After an exit-2 commit gate, a bare `tusk test-precheck` replays the
exact failed command recorded for the current task and HEAD, so unrelated
dirty paths cannot redirect diagnosis to another path/domain suite.
`--command`, `--paths`, and `--domain` explicitly bypass that replay.
Without an eligible replay, it resolves path command → domain command →
global command → test-detect. It stashes any local changes safely under a
uniquely-named entry, runs the test against HEAD, and pops that entry by
reference. Output is JSON:
`{verdict, pre_existing, exit_code, test_command, stashed,
diverged_from_default, diverged_paths}`, where `verdict` is
`non_reproduced`, `pre_existing`, `flaky`, or `skipped`, plus
`{flake_runs_total, flake_failures, flaky_suspect}` when
`--flake-retries N` (N>0) was passed.

Branch on the verdict in this order — `flaky_suspect` first, then
`verdict: non_reproduced`, then divergence, then `pre_existing: true`:

- **If `flaky_suspect` is `true`** — the N+1 HEAD runs disagreed on
  identical code, so the test is flaky, not a regression you
  introduced (issue #1076). Do not enter the diagnosis loop and do
  not conclude the failure is pre-existing. Just retry the same
  `tusk commit` with the same arguments (up to 3 times); a flake
  usually passes on the next attempt. If it keeps flapping, log a
  progress note naming the flaky test and surface it rather than
  force-committing.

- **If `verdict` is `non_reproduced` (or `exit_code` is `0` in a
  legacy payload)** — every clean-HEAD run passed, so the original
  commit-gate failure was not reproduced. Do not infer that the task
  changes introduced the failure from `pre_existing: false` alone.
  **Retry the same `tusk commit`** with the same arguments. Retry up
  to 3 times; if the original gate keeps failing while clean-HEAD
  prechecks consistently return `non_reproduced`, then use the full
  original gate output to diagnose the task changes.

- **If `pre_existing` is `true` AND `diverged_from_default` is
  `true`** — `origin/<default>` has commits HEAD lacks that touch
  the failing files (issue #1082), so the failure may already be
  fixed upstream (`diverged_paths` samples the overlap). Do not
  conclude pre-existing yet. Rebase onto the default branch first,
  then re-run the precheck:
  ```bash
  tusk sync-main                       # from the primary checkout
  git rebase origin/<default>          # from the task worktree
  tusk test-precheck --flake-retries 2
  ```
  If the refreshed precheck now reports `pre_existing: false` (or
  passes), the upstream commits carried the fix — re-run
  `tusk commit` against the rebased branch. Only if it still
  reports `pre_existing: true` with `diverged_from_default: false`
  should you treat the failure as genuinely pre-existing.

- **If `pre_existing` is `true`** (and not flaky, not still
  divergent) — the failure is unrelated to
  your changes. Skip the diagnosis loop entirely. Fall back
  immediately to:
  ```bash
  git add <file1> [file2 ...] && git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: <executing agent name and email>"
  ```
  Then mark criteria done with `tusk criteria done <cid> --skip-verify`.

- **If repeated original gates fail while clean-HEAD prechecks
  consistently return `non_reproduced`** — the combined evidence now
  points to the task changes. Proceed with the diagnosis loop:
  1. Read the full test output — scroll through the entire failure
     log. Do not make any code changes until you understand what
     failed and why.
  2. Trace the root cause — open the relevant source files and
     identify the exact lines responsible.
  3. Implement a fix — make the minimal change required to address
     the root cause.
  4. Retry `tusk commit` with the same arguments.

  Repeat up to **3 times**. If tests still fail after 3 attempts,
  run `tusk skill-run cancel <run_id>`, surface the full failure
  output and a summary of what was tried, then **stop** — do not
  continue looping.

<a id="merge-recovery"></a>

## Merge recovery

**Already-merged path:** If the feature branch was previously
merged and deleted, `tusk merge` detects this automatically when
you are on the default branch — it prints `Note: TASK-<id> — no
feature branch found; already on '<branch>'. Branch was previously
merged.`, closes the session, pushes, and marks the task Done
without re-merging. If `tusk merge` exits 0 in this scenario,
proceed to retro as normal.

**Diverged branch — rebase fallback:** If `tusk merge` exits
non-zero because the feature branch has diverged from the default
branch (fast-forward-only merge not possible), run:
```bash
tusk merge <id> --session $SESSION_ID --rebase
```
`--rebase` rebases the feature branch onto the default branch
before merging. If the rebase produces conflicts, resolve them
(`git rebase --continue`) and retry.

**Not-on-default fallback:** If `tusk merge` exits non-zero with
`No branch found matching feature/TASK-<id>-* or worktree-TASK-<id>-*`
and you are NOT on the default branch, switch to the default branch first
(`git checkout <default_branch>`), then retry `tusk merge <id> --session <session_id>`.

**Sibling-worktree + no-origin fallback:** If task work happened
in a sibling worktree, the default branch is checked out in the
primary checkout, and no `origin` remote exists, `tusk merge` from
the sibling worktree cannot perform the no-checkout fast-forward.
Run the merge from the primary checkout instead:
```bash
tusk merge <id> --session $SESSION_ID
```
If that fails with a fast-forward error because the feature branch
diverged while sibling tasks were merged, retry from the primary
checkout with `--rebase`:
```bash
tusk merge <id> --session $SESSION_ID --rebase
```

**Partial-cleanup exit code 3:** If `tusk merge` exits **3**, the merge and task completion succeeded, but local synchronization or workspace/branch cleanup remains incomplete. Read the diagnostic to identify the remaining work. Treat this as a completed task with a cleanup warning: return to the stable checkout and run `skill-run finish`, `task-summary`, and retro as described below. Preserve unexpected local files; resolve or report the remaining cleanup separately. Do not repeat implementation or discard local files merely to clear this warning.
