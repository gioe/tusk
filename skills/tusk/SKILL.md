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

Finds the highest-priority task that is ready to work on (no incomplete dependencies) and **automatically begins working on it**.

```bash
tusk task-select
```

**Empty backlog**: If the command exits with code 1, the backlog has no ready tasks. Check why:

```bash
tusk -header -column "SELECT status, COUNT(*) as count FROM tasks GROUP BY status"
```

- If there are **no tasks at all** (or all are Done): inform the user the backlog is empty and suggest running `/create-task` to add new work.
- If there are **To Do tasks but all are blocked**: inform the user and suggest running `/tusk blocked` to see what's holding them up.
- If there are **In Progress tasks**: inform the user and suggest running `/tusk wip` to check on active work.

Do **not** suggest `/groom-backlog` or `/retro` when there are no ready tasks — those skills require an active backlog or session history to be useful.

**Complexity warning**: If the selected task has complexity **L** or **XL**, display a warning to the user before proceeding:

> **Note: This is a large task (complexity: L/XL) — expect 3+ sessions to complete.**

Then ask the user whether to proceed or request a smaller task. If the user chooses a smaller task, re-run excluding L and XL:

```bash
tusk task-select --max-complexity M
```

If no smaller task is available, inform the user and offer to proceed with the original L/XL task.

After the user confirms (or if the task is not L/XL), **immediately proceed to the "Begin Work on a Task" workflow** using the retrieved task ID. Do not wait for additional user confirmation.

### Begin Work on a Task (with task ID argument)

When called with a task ID (e.g., `/tusk 6`), begin the full development workflow:

**Follow these steps IN ORDER:**

1. **Start the task** — fetch details, check progress, create/reuse session, and set status in one call:
   ```bash
   tusk task-start <id> --force
   ```
   The `--force` flag ensures the workflow proceeds even if the task has no acceptance criteria (emits a warning rather than hard-failing). This returns a JSON blob with four keys:
   - `task` — full task row (summary, description, priority, domain, assignee, etc.)
   - `progress` — array of prior progress checkpoints (most recent first). If non-empty, the first entry's `next_steps` tells you exactly where to pick up. Skip steps you've already completed (branch may already exist, some commits may already be made). Use `git log --oneline` on the existing branch to see what's already been done.
   - `criteria` — array of acceptance criteria objects (id, criterion, source, is_completed, criterion_type, verification_spec). These are the implementation checklist. Work through them in order during implementation. Mark each criterion done (`tusk criteria done <cid>`) as you complete it — do not defer this to the end. Non-manual criteria (type: code, test, file) run automated verification on `done`; use `--skip-verify` if needed. If the array is empty, proceed normally using the description as scope.
   - `session_id` — the session ID to use for the duration of the workflow (reuses an open session if one exists, otherwise creates a new one)

   Hold onto `session_id` from the JSON — it will be passed to `tusk merge` in step 12 to close the session. **Do not pass it to `tusk task-done`; use `tusk merge` for the full finalization sequence.**

2. **Create a new git branch IMMEDIATELY** (skip if resuming and branch already exists):
   ```bash
   tusk branch <id> <brief-description-slug>
   ```
   This detects the default branch (remote HEAD → gh fallback → main), checks it out, pulls latest, and creates `feature/TASK-<id>-<slug>`. It prints the created branch name on success.

   **Partial-criteria + no-commits diagnostic:** After creating or switching to the feature branch, if the `criteria` from step 1 show at least one completed criterion but `git log --oneline $(tusk git-default-branch)..HEAD` returns no commits, prior work may be stranded on another branch. Before exploring the codebase, run:
   ```bash
   git log --all --oneline --grep="\[TASK-<id>\]"
   ```
   (Replace `<id>` with the actual task ID.) This searches all branches for commits referencing the task. If commits appear on another branch (e.g., a recycled or prior branch), that branch contains the prior work — switch to it or cherry-pick the relevant commits before proceeding to Explore. If no commits appear anywhere, the criteria completions were marked without corresponding code — proceed normally and implement from scratch.

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
   3. **If tests pass**: the issue may already be fixed or the description may be inaccurate — surface this to the user and stop before investigating further.
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
       This runs `tusk lint` (advisory — never blocks), stages the listed files, commits with the `[TASK-<id>] <message>` format and Co-Authored-By trailer, and marks the criterion done — all in one call. The criterion is bound to the new commit hash automatically.

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

    **If `tusk commit` fails with `pathspec did not match any files`** (exit code 3, git-add error), first check whether the file was already committed in a prior `tusk commit` call for this task (e.g., when all changes go into a single file committed with earlier criteria), or whether the file was removed via `git rm` (which stages the deletion — `tusk commit` then can't find the path to re-add). In either case, `git add && git commit` would also fail — just mark the remaining criteria done directly:
    ```bash
    tusk criteria done <cid> --skip-verify
    ```
    If the error is a genuine pathspec mismatch (not an already-committed file), always pass file paths relative to the repo root (e.g., `ios/SomeFile.swift`, not `SomeFile.swift` from inside `ios/`). If the error persists, fall back to:
    ```bash
    git add "<file1>" ["<file2>" ...] && git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
    ```
    Then mark criteria done with `tusk criteria done <cid> --skip-verify` as usual.

    **If `tusk commit` hard-fails because tests fail** (exit code 2 — `test_command` is set and returned non-zero), **first verify the failure is not pre-existing** before entering the diagnosis loop:

    **Pre-existing failure check** — run the tests against a clean stash:
    ```bash
    git stash && <test_command>; git stash pop
    ```
    Use `tusk test-detect` to retrieve `<test_command>` if you don't already have it.

    - **If tests fail on the clean stash** — the failure is pre-existing and unrelated to your changes. **Skip the diagnosis loop entirely.** Do not attempt to fix tests in files you did not modify during this session. Fall back immediately to:
      ```bash
      git add <file1> [file2 ...] && git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
      ```
      Then mark criteria done with `tusk criteria done <cid> --skip-verify`.

    - **If tests pass on the clean stash** — your changes introduced the failure. Proceed with the diagnosis loop below. Do **not** modify any code until you've completed steps 1–2:
    1. **Read the full test output** — scroll through the entire failure log. Do not make any code changes until you understand what failed and why.
    2. **Trace the root cause** — open the relevant source files and identify the exact lines responsible for the failure.
    3. **Implement a fix** — make the minimal change required to address the root cause.
    4. **Retry `tusk commit`** with the same arguments.

    Repeat up to **3 times**. If tests still fail after 3 attempts, surface the full failure output and a summary of what was tried to the user, then **stop** — do not continue looping.

    3. Log a progress checkpoint:
      ```bash
      tusk progress <id> --next-steps "<what remains to be done>"
      ```
    - All commits should be on the feature branch (`feature/TASK-<id>-<slug>`), NOT the default branch.

    The `next_steps` field is critical — write it as if briefing a new agent who has zero context. Include what's been done, what remains, decisions made, and the branch name.

    **Schema migration reminder:** If the commit includes changes to `bin/tusk` that add or modify a migration (inside `cmd_migrate()`), run `tusk migrate` on the live database immediately after committing.

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

      After `/review-commits` completes with verdict **APPROVED**, proceed to step 12. If verdict is **CHANGES REMAINING**, surface the unresolved items to the user and stop.

12. **Finalize — merge, push, and run retro.** Execute as a single uninterrupted sequence — do NOT pause for user confirmation between steps:
    ```bash
    tusk merge <id> --session $SESSION_ID
    ```
    `tusk merge` closes the session, merges the feature branch into the default branch, pushes, deletes the feature branch, and marks the task Done. It returns JSON including an `unblocked_tasks` array. If there are newly unblocked tasks, note them in the retro.

    **Already-merged path:** If the feature branch was previously merged and deleted (e.g. via a PR that was merged in another session), `tusk merge` detects this automatically when you are on the default branch — it prints `Note: TASK-<id> — no feature branch found; already on '<branch>'. Branch was previously merged.`, closes the session, pushes, and marks the task Done without re-merging. If `tusk merge` exits 0 in this scenario, proceed to `/retro` as normal.

    **Not-on-default fallback:** If `tusk merge` exits non-zero with `No branch found matching feature/TASK-<id>-*` and you are NOT on the default branch, switch to the default branch first (`git checkout <default_branch>`), then retry `tusk merge <id> --session <session_id>`.

    **PR mode:** If the project uses PR-based merges (`merge.mode = pr` in config, or when passing `--pr`), use:
    ```bash
    tusk merge <id> --session $SESSION_ID --pr --pr-number <N>
    ```
    This squash-merges via `gh pr merge` instead of a local fast-forward.

    Then run `/retro` immediately — do not ask "shall I run retro?". Invoke it to review the session, surface process improvements, and create follow-up tasks.

### Other Subcommands

If the user invoked a subcommand (e.g., `/tusk done`, `/tusk list`, `/tusk blocked`), read the reference file:

```
Read file: <base_directory>/SUBCOMMANDS.md
```

Skip this section when running the default workflow (no subcommand argument).
