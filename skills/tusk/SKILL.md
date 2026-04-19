---
name: tusk
description: Get the most important task that is ready to be worked on
allowed-tools: Bash, Task, Read, Edit, Write, Grep, Glob
---

# Tusk Skill

The primary interface for working with tasks from the project task database (via `tusk` CLI). Use this to get the next task, start working on it, and manage the full development workflow.

> Use `/create-task` for task creation — handles decomposition, deduplication, criteria, and deps. Use `tusk task-insert` only for bulk/automated inserts.

## Setup: Discover Project Config

Before any operation that needs domain or agent values, run:

```bash
tusk config
```

This returns the full config as JSON (domains, agents, task_types, priorities, complexity, etc.). Use the returned values (not hardcoded ones) when validating or inserting tasks.

## Commands

### Get Next Task (default - no arguments)

Finds the highest-priority task that is ready to work on (no incomplete dependencies), opens a session for it, flips its status to In Progress, and returns the same JSON blob documented under "Begin Work on a Task" below — all in one call.

```bash
tusk task-start --force
```

The `--force` flag ensures the workflow proceeds even if the task has no acceptance criteria (emits a warning rather than hard-failing).

**Empty backlog**: If the command exits with code 1, the backlog has no ready tasks. Check why:

```bash
tusk -header -column "SELECT status, COUNT(*) as count FROM tasks GROUP BY status"
```

- If there are **no tasks at all** (or all are Done): inform the user the backlog is empty and suggest running `/create-task` to add new work.
- If there are **To Do tasks but all are blocked**: inform the user and suggest running `/tusk blocked` to see what's holding them up.
- If there are **In Progress tasks**: inform the user and suggest running `/tusk wip` to check on active work.

Do **not** suggest `/groom-backlog` or `/retro` when there are no ready tasks — those skills require an active backlog or session history to be useful.

On success, the JSON blob's `task.id` is the task you just started. Begin skill-run cost tracking for it and **immediately proceed to step 1b of the "Begin Work on a Task" workflow** (the initial `tusk task-start <id>` call is already done — you have the JSON). Do not wait for additional user confirmation.

```bash
tusk skill-run start tusk --task-id <id>
```

### Begin Work on a Task (with task ID argument)

When called with a task ID (e.g., `/tusk 6`), begin the full development workflow. When called with no argument, the "Get Next Task" step above has already run both `tusk task-start --force` **and** `tusk skill-run start` for you — **skip Step 1 entirely and pick up at Step 1b (Workflow routing)**, using the JSON blob and the `run_id` you already captured.

**Follow these steps IN ORDER:**

1. **Start the task and begin cost tracking** — fetch details, check progress, create/reuse session, and set status in one call:
   ```bash
   tusk task-start <id> --force
   ```
   The `--force` flag ensures the workflow proceeds even if the task has no acceptance criteria (emits a warning rather than hard-failing). This returns a JSON blob with four keys:
   - `task` — full task row (summary, description, priority, domain, assignee, etc.)
   - `progress` — array of prior progress checkpoints (most recent first). If non-empty, the first entry's `next_steps` tells you exactly where to pick up. Skip steps you've already completed (branch may already exist, some commits may already be made). Use `git log --oneline` on the existing branch to see what's already been done.
   - `criteria` — array of acceptance criteria objects (id, criterion, source, is_completed, criterion_type, verification_spec). These are the implementation checklist. Work through them in order during implementation. Mark each criterion done (`tusk criteria done <cid>`) as you complete it — do not defer this to the end. Non-manual criteria (type: code, test, file) run automated verification on `done`; use `--skip-verify` if needed. If the array is empty, proceed normally using the description as scope.
   - `session_id` — the session ID to use for the duration of the workflow (reuses an open session if one exists, otherwise creates a new one)

   Hold onto `session_id` from the JSON — it will be passed to `tusk merge` in step 12 to close the session. **Do not pass it to `tusk task-done`; use `tusk merge` for the full finalization sequence.**

   Once `tusk task-start` returns successfully, begin skill-run cost tracking so this session's spend can be attributed to the task:
   ```bash
   tusk skill-run start tusk --task-id <id>
   ```
   This prints `{"run_id": N, "started_at": "...", "task_id": N}`. Capture `run_id` — it's referenced by every exit path below.

   > **Early-exit cleanup:** If any step below causes the skill to stop before reaching the final `/retro` invocation in Step 12, first call `tusk skill-run cancel <run_id>` to close the open row, then stop. Otherwise the row lingers as `(open)` in `tusk skill-run list` forever. The explicit cancel calls below cover the known post-start early-exit paths; if you hit an unexpected bail-out, cancel before returning.
   >
   > **Pre-start exits don't need cancel.** If the "Get Next Task" flow exits 1 (empty backlog — `tusk task-start --force` returned "No ready tasks found"), that happens before `tusk skill-run start` has run, so no `run_id` exists to cancel. Just stop.

1b. **Workflow routing** — If the task's `workflow` field (from the `task` object in step 1) is non-null, the task uses a custom workflow instead of the default development cycle. Look up the corresponding skill:
   ```
   Read file: .claude/skills/<workflow>/SKILL.md
   ```
   If the file exists, cancel the /tusk skill-run (the handoff skill will open its own run) and **stop following the steps below**, following that skill's instructions instead, passing the task ID and session_id from step 1:
   ```bash
   tusk skill-run cancel <run_id>
   ```
   If the file does not exist, log a warning ("Workflow '<workflow>' not found — falling back to default development cycle") and continue with step 2 (no cancel — the /tusk run stays open for the rest of the default flow).

2. **Create a new git branch IMMEDIATELY** (skip if resuming and branch already exists):
   ```bash
   tusk branch <id> <brief-description-slug>
   ```
   This detects the default branch (remote HEAD → gh fallback → main), checks it out, pulls latest, and creates `feature/TASK-<id>-<slug>`. It prints the created branch name on success.

   **Deliverable check:** If `deliverable_check_needed` from step 1 is `true`, run:
   ```bash
   tusk check-deliverables <id>
   ```
   (Replace `<id>` with the actual task ID.) This command checks all branches for commits referencing the task and, if none are found, scans the task description and criteria for referenced file paths and tests whether they exist on disk. Act on the `recommendation` field:
   - **`"commits_found"`** — commits exist on another branch. Switch to it or cherry-pick the relevant commits before proceeding to Explore.
   - **`"mark_done"`** — no commits, but deliverable files listed in `files` already exist on disk. Mark all criteria done with `--skip-verify` and proceed directly to step 9 (commit + merge) without reimplementing.
   - **`"implement_fresh"`** — no commits and no deliverable files found. The criteria completions were marked without corresponding code — proceed normally and implement from scratch.

3. **Determine the best subagent(s)** based on:
   - Task domain
   - Task assignee field (often indicates the right agent type)
   - Task description and requirements

4. **Confirm failure** — Run the failing tests *before* exploring any code when the task is about *fixing* an existing failure. This confirms the bug still exists and avoids wasted investigation.

   **When to run this step:**
   - `task_type: bug` → always run
   - `task_type: test` AND the summary/description indicates fixing a failing or flaky test → run
   - `task_type: test` AND the summary/description indicates *writing new tests* (no existing failure to reproduce, e.g. "Add tests for X", "Write test suite for Y") → **skip this step entirely and proceed to Explore**
   - All other task types (feature, chore, docs, etc.) → skip

   1. Check the task description and acceptance criteria for specific test commands or test names to run.
   2. If specific tests are named, run them directly. Otherwise, use `tusk test-detect` to find the project's test command, then run the most relevant subset.
   3. **If tests pass**: the issue may already be fixed or the description may be inaccurate — run `tusk skill-run cancel <run_id>`, surface this to the user, and stop before investigating further.
   4. **If tests fail**: capture the failure output. Use it as the primary diagnostic anchor in step 5 (Explore).

5. **Explore the codebase before implementing** — use a sub-agent to research:
   - What files will need to change?
   - Are there existing patterns to follow?
   - What tests already exist for this area?
   - **For each file you plan to modify**, grep it for keywords related to the feature (e.g., the concept name, the config key, the resource type). If a helper function already exists that covers what you're about to write, use it instead of duplicating the logic.

   Report findings before writing any code.

5. **Scope check — only implement what the task describes.**
   The task's `summary` and `description` fields define the full scope of work for this session. If the description references or links to external documents (evaluation docs, design specs, RFCs), treat them as **background context only** — do not implement items from those docs that go beyond what the task's own description asks for. Referenced docs often describe multi-task plans; implementing the entire plan collapses future tasks into one PR and defeats dependency ordering.

6. **Delegate the work** to the chosen subagent(s).

7. **Implement, commit, and mark criteria done.** Work through the acceptance criteria from step 1 as your checklist — **one commit per criterion is the default**. For each criterion in order:
    1. Implement the changes that satisfy it
    2. Commit and mark the criterion done atomically using `tusk commit --criteria`:
       ```bash
       tusk commit <id> "<message>" "<file1>" ["<file2>" ...] --criteria <cid>
       ```
       An alternative `-m` flag form is also supported (useful when file paths come first):
       ```bash
       tusk commit <id> "<file1>" ["<file2>" ...] -m "<message>" --criteria <cid>
       ```
       This runs `tusk lint` (advisory — never blocks), stages the listed files, commits with the `[TASK-<id>] <message>` format and Co-Authored-By trailer, and marks the criterion done — all in one call. The criterion is bound to the new commit hash automatically. Duplicate `[TASK-N]` prefixes in the message are stripped automatically, and bare `--` separators are silently ignored.

       **Always quote file paths** — zsh expands unquoted brackets (`[id]`, `[slug]`) as glob patterns before the shell passes arguments to `tusk commit`. Any path component containing `[`, `]`, `*`, `?`, or spaces must be wrapped in double quotes (e.g., `"apps/api/[id]/route.ts"`).

       **Grouping criteria:** 2–3 genuinely co-located criteria (e.g., a schema change and its migration) may share one commit — use one `--criteria` flag per ID:
       ```bash
       tusk commit <id> "<message>" "<file1>" ["<file2>" ...] --criteria <cid1> --criteria <cid2>
       ```
       Always include a brief rationale in the commit message when grouping. **Never** bundle all criteria onto a single end-of-task commit.

    **If the task has no git-trackable file changes** (e.g., a venv install, a runtime config change, an OS-level operation), skip `tusk commit` entirely — it requires at least one file argument and will fail with exit code 1 (usage error) if none are provided. Mark criteria done directly:
    ```bash
    tusk criteria done <cid> --skip-verify
    ```

    **After each `tusk commit` in foreground mode**, run `git status --short` to confirm your files were staged and committed — a zero-exit commit that produced no diff (e.g. all files were already tracked with no changes) will silently succeed without staging anything.

    **If `tusk commit` fails with `pathspec did not match any files`** (exit code 3, git-add error), first check whether the file was already committed in a prior `tusk commit` call for this task (e.g., when all changes go into a single file committed with earlier criteria), or whether the file was removed via `git rm` (which stages the deletion — `tusk commit` then can't find the path to re-add). In either case, `git add && git commit` would also fail — just mark the remaining criteria done directly:
    ```bash
    tusk criteria done <cid> --skip-verify
    ```
    If the error is a genuine pathspec mismatch (not an already-committed file), always pass file paths relative to the repo root (e.g., `ios/SomeFile.swift`, not `SomeFile.swift` from inside `ios/`). If the error persists, fall back to:
    ```bash
    git add "<file1>" ["<file2>" ...] && git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
    ```
    Then mark criteria done with `tusk criteria done <cid> --skip-verify` as usual.

    **If `tusk commit` fails with `pathspec '…' is beyond a symbolic link`** (exit code 3), the path lives under a symlinked directory that `git add` refuses to traverse. In tusk's own repo this hits any path under `.claude/skills/<name>/`, because each skill is a symlink to `skills/<name>/`. Retry with the real source path:
    ```bash
    tusk commit <id> "<message>" "skills/<name>/SKILL.md" --criteria <cid>
    ```
    More generally: if `ls -la` on any parent directory shows it is a symlink, use the link's target path instead.

    **If a pre-commit auto-formatter (e.g. `black`, `ruff --fix`, `prettier`, `gofmt`) rewrites a staged file in-place**, `tusk commit` detects the index/working-tree divergence, re-stages the reformatted content, and retries the commit exactly once — no manual intervention required. If the retry also fails (the formatter produces unstable output on every run), bypass hooks with:
    ```bash
    tusk commit <task_id> "<message>" "<file>" --skip-verify
    ```

    **If the commit removes a file from git tracking** (i.e., the staged change is a `git rm --cached` deletion, not a file modification), do NOT use `tusk commit` — it retries gitignored paths with `git add -f`, which re-adds the file and defeats the deletion. Use `git commit` directly:
    ```bash
    git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
    ```
    Then mark criteria done with `tusk criteria done <cid> --skip-verify`.

    **If `tusk commit` exits 6 (blocking lint violation)** — the commit did NOT land. A non-advisory lint rule fired (Rule 1 raw sqlite3, Rule 3 hardcoded DB path, Rule 11 bad SKILL.md frontmatter, Rule 16 DB-backed blocking rules, Rules 18/19 MANIFEST drift, Rule 21 multi-trailing-newlines, etc.). The violating rule's output is printed verbatim — fix it, then retry `tusk commit`. Advisory-only rules (Rule 13 VERSION bump missing, Rule 15 big-bang commits, Rule 17 DB-backed advisory, etc.) still print WARN lines but do NOT exit non-zero and do NOT block. If the violation is a known false positive or pre-existing state you can't resolve in this commit, bypass with `--skip-lint` (lint only) or widen to `--skip-verify` (lint, tests, and pre-commit hooks):
    ```bash
    tusk commit <id> "<message>" "<file>" --skip-lint --criteria <cid>
    ```
    Lint output during commit is now filtered: only rules with violations print — passing rules are suppressed. If the last lint pass was clean, you won't see any lint output at all.

    **If `tusk commit` hard-fails because tests fail** (exit code 2 — `test_command` is set and returned non-zero), **first verify the failure is not pre-existing** before entering the diagnosis loop:

    **Pre-existing failure check** — run the tests against HEAD with any local changes safely set aside:
    ```bash
    tusk test-precheck
    ```
    Or pass an explicit command when the config-resolved one isn't what you want to check against:
    ```bash
    tusk test-precheck --command "<test_command>"
    ```
    `tusk test-precheck` resolves the test command from `--command`, then `config.test_command`, then `tusk test-detect`. When the working tree is dirty it stashes local changes under a *uniquely-named* entry, runs the test against HEAD, and pops *that entry by reference* — never by top-of-stack. When the working tree is clean it runs the test directly without touching `git stash` at all. Output is JSON on stdout: `{pre_existing, exit_code, test_command, stashed}`; the test command's own output is redirected to stderr so programmatic callers can `json.loads(stdout)` directly. Do **not** fall back to the raw `git stash && … ; git stash pop` snippet — when the tree is clean, the empty `git stash` becomes a no-op and `git stash pop` will pop a stale foreign entry and silently trash unrelated state. If precheck exits non-zero, it prints a recovery message on stderr (always including the stash message, when one was created) so you can finish the pop manually; it never silently falls through with changes orphaned in the stash list.

    - **If `pre_existing` is `true`** — the failure is pre-existing and unrelated to your changes. **Skip the diagnosis loop entirely.** Do not attempt to fix tests in files you did not modify during this session. Fall back immediately to:
      ```bash
      git add <file1> [file2 ...] && git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
      ```
      Then mark criteria done with `tusk criteria done <cid> --skip-verify`.

    - **If `pre_existing` is `false`** — your changes introduced the failure. Proceed with the diagnosis loop below. Do **not** modify any code until you've completed steps 1–2:
    1. **Read the full test output** — scroll through the entire failure log. Do not make any code changes until you understand what failed and why.
    2. **Trace the root cause** — open the relevant source files and identify the exact lines responsible for the failure.
    3. **Implement a fix** — make the minimal change required to address the root cause.
    4. **Retry `tusk commit`** with the same arguments.

    Repeat up to **3 times**. If tests still fail after 3 attempts, run `tusk skill-run cancel <run_id>`, surface the full failure output and a summary of what was tried to the user, then **stop** — do not continue looping.

    3. Log a progress checkpoint:
      ```bash
      tusk progress <id> --next-steps "<what remains to be done>"
      ```
    - All commits should be on the feature branch (`feature/TASK-<id>-<slug>`), NOT the default branch.

    The `next_steps` field is critical — write it as if briefing a new agent who has zero context. Include what's been done, what remains, decisions made, and the branch name.

    **Schema migration reminder:** If the commit adds or modifies a migration in `bin/tusk-migrate.py` (or bumps `cmd_init`'s fresh-DB `user_version` stamp in `bin/tusk`), run `tusk migrate` on the live database immediately after committing.

8. **Review the code locally** before considering the work complete.

9. **Verify all acceptance criteria are done** before pushing:
    ```bash
    tusk criteria list <id>
    ```
    If any criteria are still incomplete, address them now. If a criterion was intentionally skipped, note why in the PR description.

10. **Run convention lint (advisory)** — `tusk commit` already runs lint before each commit. If you need to check lint independently before pushing:
    ```bash
    tusk lint
    ```
    Review the output. This check is **advisory only** — violations are warnings, not blockers. Fix any clear violations in files you've already touched. Do not refactor unrelated code just to satisfy lint.

11. **Run `/review-commits`** — check the review mode first:
    ```bash
    tusk config review
    ```
    - **mode = disabled** (or review key missing): skip review, proceed to step 12.
    - **mode = ai_only**: run `/review-commits` by following the instructions in:
      ```
      Read file: <base_directory>/../review-commits/SKILL.md for task <id>
      ```
      > **Warning:** Do NOT spawn a `pr-review-toolkit:code-reviewer` agent directly as a shortcut. That agent receives only a manually reconstructed diff — not the real `git diff` output — which causes false-positive review findings. The `/review-commits` skill exists specifically to fetch and pass the real diff verbatim; bypassing it removes that safeguard.

      After `/review-commits` completes with verdict **APPROVED**, proceed to step 12. If verdict is **CHANGES REMAINING**, run `tusk skill-run cancel <run_id>`, surface the unresolved items to the user, and stop.

12. **Finalize — merge, push, and run retro.** Execute as a single uninterrupted sequence — do NOT pause for user confirmation between steps:
    ```bash
    tusk merge <id> --session $SESSION_ID
    ```
    `tusk merge` closes the session, merges the feature branch into the default branch, pushes, deletes the feature branch, and marks the task Done. It returns JSON including an `unblocked_tasks` array. If there are newly unblocked tasks, note them in the retro.

    **Already-merged path:** If the feature branch was previously merged and deleted (e.g. via a PR that was merged in another session), `tusk merge` detects this automatically when you are on the default branch — it prints `Note: TASK-<id> — no feature branch found; already on '<branch>'. Branch was previously merged.`, closes the session, pushes, and marks the task Done without re-merging. If `tusk merge` exits 0 in this scenario, proceed to `/retro` as normal.

    **Diverged branch — rebase fallback:** If `tusk merge` exits non-zero because the feature branch has diverged from the default branch (fast-forward-only merge not possible), run:
    ```bash
    tusk merge <id> --session $SESSION_ID --rebase
    ```
    `--rebase` rebases the feature branch onto the default branch before merging. If the rebase produces conflicts, resolve them (`git rebase --continue`) and retry.

    **Not-on-default fallback:** If `tusk merge` exits non-zero with `No branch found matching feature/TASK-<id>-*` and you are NOT on the default branch, switch to the default branch first (`git checkout <default_branch>`), then retry `tusk merge <id> --session <session_id>`.

    **PR mode:** If the project uses PR-based merges (`merge.mode = pr` in config, or when passing `--pr`), use:
    ```bash
    tusk merge <id> --session $SESSION_ID --pr --pr-number <N>
    ```
    This squash-merges via `gh pr merge` instead of a local fast-forward.

    **No-commit closure (`wont_do` / `duplicate`):** If the task should be closed *without* shipping any code — an evaluation/spike whose answer is "don't do it", or a task that turns out to be a duplicate — use `tusk abandon` instead of `tusk merge`:
    ```bash
    tusk abandon <id> --reason wont_do|duplicate --session $SESSION_ID [--note "<rationale>"]
    ```
    `tusk abandon` switches off the feature branch, deletes it (force), closes the session, and marks the task Done with the given `closed_reason` in one call. **Refuses** if the feature branch has commits not on the default branch — in that case use `tusk merge` to ship the work, or delete the branch manually if you really want to discard it. The optional `--note` records the decision rationale on `task_progress` so the audit trail survives. After `tusk abandon` exits 0, run `/retro` exactly as you would after `tusk merge`.

    After `tusk merge` (or `tusk abandon`) exits 0, close out the /tusk skill-run so its cost is captured before `/retro` starts its own run:
    ```bash
    tusk skill-run finish <run_id>
    ```

    Then run `/retro` immediately — do not ask "shall I run retro?". Invoke it to review the session, surface process improvements, and create follow-up tasks.

### Other Subcommands

If the user invoked a subcommand (e.g., `/tusk done`, `/tusk list`, `/tusk blocked`), read the reference file:

```
Read file: <base_directory>/SUBCOMMANDS.md
```

Skip this section when running the default workflow (no subcommand argument).
