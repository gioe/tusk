# Tusk — Conditional Recovery

Read only the section selected by the core workflow trigger. These instructions
retain that workflow’s gates and route back to its numbered steps.

<a id="deliverable-check"></a>

## Deliverable-check recommendations

**Deliverable check:** If `deliverable_check_needed` from step 1 is `true`, run:
```bash
tusk check-deliverables <id>
```
(Replace `<id>` with the actual task ID.) This command checks all branches for commits referencing the task and, if none are found, scans the task description and criteria for referenced file paths and tests whether they exist on disk. Act on the `recommendation` field:
- **`"commits_found"`** — `[TASK-<id>]` commits exist on a non-default branch (typically a stale feature branch from a prior session). Switch to it or cherry-pick the relevant commits before proceeding to Explore.
- **`"merged_not_closed"`** — `[TASK-<id>]` commits already exist on the default branch AND their diff overlaps with files referenced in this task (or there is no scope signal to compare against). Treat as the orphaned-task case: work was merged without being finalized through `tusk merge`. The SHAs are listed in `default_branch_commits`. Skip implementation entirely. Mark all criteria done with `--skip-verify`, then jump straight to step 12 to close out the session — `tusk merge` will detect the already-merged state and finalize without re-merging.
- **`"merged_not_closed_low_confidence"`** — `[TASK-<id>]` commits exist on the default branch but their diff (listed in `default_branch_commit_files`) does NOT overlap with files referenced in this task's description / acceptance criteria / verification specs, NOR with files modified on any `[TASK-<id>]` commit on a feature branch. This is the prefix-match false-positive case (issue #606, original incident TASK-1691): another task's commit was likely tagged with this task's `[TASK-N]` prefix by mistake. Only fires for legacy tasks (`scope_enforced=0`) — TASK-472 short-circuits this branch for `scope_enforced=1` tasks, which return `merged_not_closed` instead because the commit-time scope guard already filtered out-of-scope writes (consult `tusk scope list <id>` to see the authorized `task_scope` record). **Verify before acting** (legacy path only) — inspect each commit listed in `default_branch_commits` (`git show <sha>`) and confirm whether it actually represents this task's work. If yes, treat as `merged_not_closed` (skip implementation, jump to step 12). If no, ignore the on-default commits and proceed normally with Explore → Implement as if the recommendation were `implement_fresh`.
- **`"mark_done"`** — no commits, but deliverable files listed in `files` already exist on disk AND at least one non-deferred criterion has a non-`manual` `criterion_type` AND the verification-spec gate passed (all runnable positive specs pass, or no runnable spec exists). Negative/absence specs never count as positive convergence evidence; inspect the positive/negative spec counts alongside the legacy total counts. Mark all criteria done with `--skip-verify` and proceed directly to step 9 (commit + merge) without reimplementing.
- **`"manual_pending"`** — no commits, deliverable files exist on disk, BUT every non-deferred criterion is `criterion_type='manual'` (issue #806). File existence is **noise** for manual criteria — a referenced gitignored file may exist regardless of whether the operator performed the external work (the original incident was an OAuth secret-rotation task whose deliverable lived in Google Cloud Console / Apple Developer / Vercel, not in the repo). **Do NOT auto-close.** Proceed normally with Explore → Implement; the human has to actually do the manual steps, then mark each criterion done explicitly.
- **`"criteria_complete_no_commits"`** — every non-deferred acceptance criterion is already marked `is_completed=1`, but there are no `[TASK-<id>]` commits anywhere AND no deliverable files on disk. This is a **salvage / converged-work / speculative-mark** signal (issue #578, original incident TASK-1714): a prior session marked criteria done without producing any committed deliverable. Common causes: (1) lost-work — a prior agent did real work but couldn't commit cleanly (dirty worktree, branch protection, bundled unrelated changes on a salvage branch); (2) convergent-evolution — separate tasks effectively achieved the goal, so no fresh commits are needed for THIS task; (3) speculative pre-marking — criteria were marked done at the start of a prior session without backing code. **Do NOT silently proceed as `implement_fresh`.** Instead: (a) read the task's progress notes via `tusk task-get <id>` and inspect any `next_steps` references; (b) `git branch -a | grep TASK-<id>` for stale branches and inspect their diff against the default branch (`git log <branch>..origin/<default>` and `git show <sha>`) to determine whether the work is obsolete vs. still relevant; (c) surface the options to the user — **re-implement** (proceed with Explore → Implement as if `implement_fresh`), **accept-as-converged** (close via `tusk abandon <id> --reason completed --note "<rationale referencing the converging task or commits>"`), or **abandon** (close via `tusk abandon <id> --reason wont_do --note "..."`). Do not pick the path unilaterally.
- **`"implement_fresh"`** — no commits and either no deliverable files were found, or files exist but every incomplete code/file verification spec still fails (issue #1068: the deliverable is an EDIT to an existing referenced file, so file existence is noise — `files_found` stays `true` and `verifiable_spec_count` > 0 with `passing_spec_count` = 0 records the downgrade). Proceed normally and implement from scratch.

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

**If `tusk commit` exits 9 (concurrent commit active)**, another invocation holds the operation lock for the same worktree and this process did not run `git commit`. Wait for the active invocation to finish and inspect its `TUSK_COMMIT_RESULT`. Retry only when that result shows the requested commit did not land. If the result is unavailable, inspect HEAD with `git log -1 --format='%H %s'`, inspect the selected paths with `git status --short -- "<file1>" ["<file2>" ...]`, and inspect criterion bindings with `tusk criteria list <id>` before deciding. If the requested TASK commit and intended criterion bindings landed and the selected requested changes are clean, do not reissue `tusk commit`; if the evidence is inconsistent, investigate instead of retrying blindly. Do not interpret exit 9 as a Git failure or mark criteria directly from the losing invocation.

**Raw Git fallback attribution:** Replace the attribution placeholder in the examples below with the executing agent's known name and email in Git's standard `Name <email>` form; do not copy a historical model identity or infer it from this prompt's filename. If the identity is unavailable, omit the trailer and report the limitation rather than guessing. This guidance applies to raw Git fallbacks; the normal `tusk commit` command supplies its own trailer.

**If `tusk commit` fails with `pathspec did not match any files`** (exit code 3, git-add error), first check whether the file was already committed in a prior `tusk commit` call for this task (e.g., when all changes go into a single file committed with earlier criteria), or whether the file was removed via `git rm` (which stages the deletion — `tusk commit` then can't find the path to re-add). In either case, `git add && git commit` would also fail — just mark the remaining criteria done directly:
```bash
tusk criteria done <cid> --skip-verify
```
If the error is a genuine pathspec mismatch (not an already-committed file), always pass file paths relative to the repo root (e.g., `ios/SomeFile.swift`, not `SomeFile.swift` from inside `ios/`). If the error persists, fall back to a path-limited commit:
```bash
git add -- "<file1>" ["<file2>" ...]
git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: <executing agent name and email>" -o -- "<file1>" ["<file2>" ...]
```
`git commit -o -- <files>` limits the commit to the listed paths so unrelated pre-staged changes cannot leak into the task commit. Then mark criteria done with `tusk criteria done <cid> --skip-verify` as usual.

**If `tusk commit` fails with `pathspec '…' is beyond a symbolic link`** (exit code 3), the path lives under a symlinked directory that `git add` refuses to traverse. In tusk's own repo this hits any path under `.claude/skills/<name>/`, because each skill is a symlink to `skills/<name>/`. Retry with the real source path:
```bash
tusk commit <id> "<message>" "skills/<name>/SKILL.md" --criteria <cid>
```
More generally: if `ls -la` on any parent directory shows it is a symlink, use the link's target path instead.

**If a pre-commit auto-formatter (e.g. `black`, `ruff --fix`, `prettier`, `gofmt`) rewrites a staged file in-place**, `tusk commit` detects the index/working-tree divergence, re-stages the reformatted content, and retries the commit exactly once — no manual intervention required. If the retry also fails (the formatter produces unstable output on every run), bypass hooks with:
```bash
tusk commit <task_id> "<message>" "<file>" --skip-verify
```

**If the commit removes a file from git tracking** (any staged deletion — `git rm <file>`, `git rm --cached <file>`, or `rm <file>` followed by `git add <file>` — all produce identical `deleted: <path>` index entries), do NOT use `tusk commit` — it retries gitignored paths with `git add -f`, which re-adds the file and defeats the deletion. Use `git commit` directly:
```bash
git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: <executing agent name and email>"
```
Then mark criteria done with `tusk criteria done <cid> --skip-verify`.

**If `tusk commit` exits 5 (test_command timeout)** — the configured `test_command` exceeded its timeout and was killed before producing an exit code. The stderr message names the resolved timeout and source. The resolution chain is `TUSK_TEST_COMMAND_TIMEOUT` env var > `config.test_command_timeout_sec` in `tusk/config.json` > default (240s). If the failure is just slow first-run compilation (cold xcodebuild, Bazel cold cache, large Rust compile), retry with a per-invocation override:
```bash
TUSK_TEST_COMMAND_TIMEOUT=600 tusk commit <id> "<message>" "<file>" --criteria <cid>
```
If the slow path is permanent for this project, raise `test_command_timeout_sec` in `tusk/config.json` instead of overriding on every call. **Do not blindly raise the timeout** when the command genuinely hangs (e.g. waiting on interactive input or a missing dependency) — make the command non-interactive and fix the underlying hang first.

**If `tusk commit` hard-fails because tests fail** (exit code 2 — `test_command` is set and returned non-zero), **first verify the failure is not pre-existing** before entering the diagnosis loop:

**Pre-existing failure check** — run the tests against HEAD with any local changes safely set aside. **Always pass `--flake-retries N`** (use `N=2`) on this post-gate-failure precheck so flake detection actually fires — without it `flaky_suspect` is never emitted and an intermittent test reads as a real regression:
```bash
tusk test-precheck --flake-retries 2
```
Or pass an explicit command when the config-resolved one isn't what you want to check against:
```bash
tusk test-precheck --command "<test_command>" --flake-retries 2
```
After an exit-2 commit gate, a bare `tusk test-precheck` replays the exact failed command recorded for the current task and HEAD, so unrelated dirty paths cannot redirect diagnosis to another path/domain suite. `--command`, `--paths`, and `--domain` are explicit selectors and bypass that replay. Without an eligible replay, resolution remains path command → domain command → global `config.test_command` → `tusk test-detect`. When the working tree is dirty it stashes local changes under a *uniquely-named* entry, runs the test against HEAD, and pops *that entry by reference* — never by top-of-stack. When the working tree is clean it runs the test directly without touching `git stash` at all. Output is JSON on stdout: `{verdict, pre_existing, exit_code, test_command, stashed, diverged_from_default, diverged_paths}`, where `verdict` is `non_reproduced`, `pre_existing`, `flaky`, or `skipped`, plus `{flake_runs_total, flake_failures, flaky_suspect}` when `--flake-retries N` (N>0) was passed; the test command's own output is redirected to stderr so programmatic callers can `json.loads(stdout)` directly. Do **not** fall back to the raw `git stash && … ; git stash pop` snippet — when the tree is clean, the empty `git stash` becomes a no-op and `git stash pop` will pop a stale foreign entry and silently trash unrelated state. If precheck exits non-zero, it prints a recovery message on stderr (always including the stash message, when one was created) so you can finish the pop manually; it never silently falls through with changes orphaned in the stash list.

Branch on the verdict **in this order** — `flaky_suspect` first, then `verdict: non_reproduced`, then divergence, then `pre_existing: true`:

- **If `flaky_suspect` is `true`** — the N+1 HEAD runs disagreed on identical code, so the test is **flaky, not a regression you introduced** (issue #1076). Do **not** enter the diagnosis loop and do **not** conclude the failure is pre-existing. Simply **retry the same `tusk commit`** with the same arguments — the gate re-runs the test and a flake will usually pass on the next attempt. Retry up to 3 times; if it still fails *and* `flaky_suspect` stops appearing, fall through to the branches below. If it keeps flapping, log a progress note naming the flaky test and surface it to the user rather than force-committing.

- **If `verdict` is `non_reproduced` (or `exit_code` is `0` in a legacy payload)** — every clean-HEAD run passed, so the original commit-gate failure was **not reproduced**. Do not infer that the task changes introduced the failure from `pre_existing: false` alone. **Retry the same `tusk commit`** with the same arguments. Retry up to 3 times; if the original gate keeps failing while clean-HEAD prechecks consistently return `non_reproduced`, then use the full original gate output to diagnose the task changes.

- **If `pre_existing` is `true` AND `diverged_from_default` is `true`** — `origin/<default>` has commits HEAD lacks that touch the failing files (issue #1082), so the failure **may already be fixed upstream**; `diverged_paths` samples the overlapping files. Do **not** conclude pre-existing yet and do **not** file a follow-up for it. **Rebase onto the default branch first**, then re-run the precheck:
  ```bash
  tusk sync-main          # fetch + ff-only pull of origin/<default> + migrate (run from the primary checkout)
  # then, from the task worktree, bring the feature branch up to the refreshed default:
  git -C "<workspace_path>" rebase origin/<default>
  tusk test-precheck --flake-retries 2
  ```
  If the refreshed precheck now reports `pre_existing: false` (or passes), the upstream commits carried the fix — re-run `tusk commit` against the rebased branch. Only if it *still* reports `pre_existing: true` with `diverged_from_default: false` should you treat the failure as genuinely pre-existing and fall through to the next branch.

- **If `pre_existing` is `true`** (and not flaky, and not still-divergent) — the failure is pre-existing and unrelated to your changes. **Skip the diagnosis loop entirely.** Do not attempt to fix tests in files you did not modify during this session. Fall back immediately to:
  ```bash
  git add -- "<file1>" ["<file2>" ...]
  git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: <executing agent name and email>" -o -- "<file1>" ["<file2>" ...]
  ```
  Then mark criteria done with `tusk criteria done <cid> --skip-verify`. The `-o -- <files>` form is required here too; a plain `git commit` would include any unrelated paths that were staged before this task.

- **If repeated original gates fail while clean-HEAD prechecks consistently return `non_reproduced`** — the combined evidence now points to the task changes. Proceed with the diagnosis loop below. Do **not** modify any code until you've completed steps 1–2:
1. **Read the full test output** — scroll through the entire failure log. Do not make any code changes until you understand what failed and why.
2. **Trace the root cause** — open the relevant source files and identify the exact lines responsible for the failure.
3. **Implement a fix** — make the minimal change required to address the root cause.
4. **Retry `tusk commit`** with the same arguments.

Repeat up to **3 times**. If tests still fail after 3 attempts, run `tusk skill-run cancel <run_id>`, surface the full failure output and a summary of what was tried to the user, then **stop** — do not continue looping.

<a id="merge-recovery"></a>

## Merge recovery

**Already-merged path:** If the feature branch was previously merged and deleted (e.g. via a PR that was merged in another session), `tusk merge` detects this automatically when you are on the default branch — it prints `Note: TASK-<id> — no feature branch found; already on '<branch>'. Branch was previously merged.`, closes the session, pushes, and marks the task Done without re-merging. If `tusk merge` exits 0 in this scenario, proceed to `/retro` as normal.

**Diverged branch — rebase fallback:** If `tusk merge` exits non-zero because the feature branch has diverged from the default branch (fast-forward-only merge not possible), run:
```bash
tusk merge <id> --session $SESSION_ID --rebase
```
`--rebase` rebases the feature branch onto the default branch before merging. If the rebase produces conflicts, resolve them (`git rebase --continue`) and retry.

**Not-on-default fallback:** If `tusk merge` exits non-zero with `No branch found matching feature/TASK-<id>-* or worktree-TASK-<id>-*` and you are NOT on the default branch, switch to the default branch first (`git checkout <default_branch>`), then retry `tusk merge <id> --session <session_id>`.

**Sibling-worktree + no-origin fallback:** If task work happened in a sibling worktree, the default branch is checked out in the primary checkout, and no `origin` remote exists, `tusk merge` from the sibling worktree cannot perform the no-checkout fast-forward. Run the merge from the primary checkout instead:
```bash
tusk merge <id> --session $SESSION_ID
```
If that fails with a fast-forward error because the feature branch diverged while sibling tasks were merged, retry from the primary checkout with `--rebase`:
```bash
tusk merge <id> --session $SESSION_ID --rebase
```

**Partial-cleanup exit code 3:** If `tusk merge` exits **3**, the merge and task completion succeeded, but local synchronization or workspace/branch cleanup remains incomplete. Read the diagnostic to identify the remaining work. Treat this as a completed task with a cleanup warning: return to the stable checkout and run `skill-run finish`, `task-summary`, and retro as described below. Preserve unexpected local files; resolve or report the remaining cleanup separately. Do not repeat implementation or discard local files merely to clear this warning.

**Sibling-worktree DB fallback:** If the default branch is checked out in a sibling worktree and the primary checkout is unusable, run the merge from the sibling worktree while pinning tusk to the primary repo's DB:
```bash
TUSK_PROJECT=<primary_repo_path> tusk merge <id> --session $SESSION_ID --rebase
```
This is the correct fallback when running `tusk merge` from the sibling worktree fails with `no such table: task_sessions`: that worktree has the git state needed for the merge, but tusk resolved its database relative to the sibling CWD. `TUSK_PROJECT` keeps tusk pointed at the primary repo's project database while git commands operate in the current worktree.
