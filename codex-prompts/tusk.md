# Tusk — Primary Task Workflow (Codex)

The primary interface for working with tasks from the project task
database (via the `tusk` CLI). Use this prompt to get the next task,
start working on it, and manage the full development workflow.

> **Conventions:** Run `tusk conventions search <topic>` for project
> rules (commits, structure, testing, migrations, skill authoring,
> criteria shape). Do not restate convention text inline — it drifts
> from the DB.

> Use `create-task.md` for task creation — handles decomposition,
> deduplication, criteria, and deps. Use `tusk task-insert` only for
> bulk/automated inserts.

## Setup: Discover Project Config

Before any operation that needs domain or agent values, run:

```bash
tusk config
```

This returns the full config as JSON (domains, agents, task_types,
priorities, complexity, etc.). Use the returned values (not hardcoded
ones) when validating or inserting tasks.

## Commands

### Get Next Task (default — no arguments)

Finds the highest-priority task that is ready to work on (no incomplete
dependencies), opens a session for it, flips its status to In Progress,
opens a skill-run row for cost tracking, and returns the same JSON blob
documented under "Begin Work on a Task" below — all in one call.

```bash
tusk task-start --force --skill tusk
```

The `--force` flag ensures the workflow proceeds even if the task has
no acceptance criteria (emits a warning rather than hard-failing). The
`--skill tusk` flag opens a `skill_runs` row attributed to this task;
`run_id` is returned under `skill_run.run_id` in the JSON — capture it
for the cancel/finish calls later.

**Empty backlog:** If the command exits with code 1, the backlog has no
ready tasks. Check why:

```bash
tusk -header -column "SELECT status, COUNT(*) as count FROM tasks GROUP BY status"
```

- If there are **no tasks at all** (or all are Done): inform the user
  the backlog is empty and suggest running `create-task.md` to add new
  work.
- If there are **To Do tasks but all are blocked**: inform the user and
  suggest running `tusk deps blocked` to see what's holding them up.
- If there are **In Progress tasks**: inform the user and suggest
  inspecting them via
  `tusk -header -column "SELECT id, summary, priority, domain, assignee FROM tasks WHERE status = 'In Progress'"`.

Do **not** suggest `groom-backlog.md` or `retro.md` when there are no
ready tasks — those prompts require an active backlog or session
history to be useful.

On success, the JSON blob's `task.id` is the task you just started and
`skill_run.run_id` is the open skill-run row. **Immediately proceed to
Step 1b of the "Begin Work on a Task" workflow** — do not wait for
additional user confirmation.

### Begin Work on a Task (with task ID argument)

When called with a task ID (e.g., `/tusk 6`), begin the full development
workflow. When called with no argument, the "Get Next Task" step above
has already run `tusk task-start --force --skill tusk` for you — **skip
Step 1 entirely and pick up at Step 1b (Workflow routing)**, using the
JSON blob and the `skill_run.run_id` you already captured.

**Follow these steps IN ORDER:**

1. **Start the task and begin cost tracking** — fetch details, check
   progress, create/reuse session, set status, and open the skill-run
   row in one call:
   ```bash
   tusk task-start <id> --force --skill tusk
   ```
   This returns a JSON blob with these keys:
   - `task` — full task row (summary, description, priority, domain,
     assignee, etc.)
   - `progress` — array of prior progress checkpoints (most recent
     first). If non-empty, the first entry's `next_steps` tells you
     exactly where to pick up. Skip steps you've already completed
     (branch may already exist, some commits may already be made). Use
     `git log --oneline` on the existing branch to see what's already
     been done.
   - `criteria` — array of acceptance criteria objects (id, criterion,
     source, is_completed, criterion_type, verification_spec). These
     are the implementation checklist. Work through them in order
     during implementation. Mark each criterion done
     (`tusk criteria done <cid>`) as you complete it — do not defer
     this to the end. Non-manual criteria (type: code, test, file) run
     automated verification on `done`; use `--skip-verify` if needed.
     If the array is empty, proceed normally using the description as
     scope.
   - `session_id` — the session ID to use for the duration of the
     workflow (reuses an open session if one exists, otherwise creates
     a new one).
   - `skill_run` — `{run_id, skill_name, started_at, task_id}` for the
     skill-run row opened by `--skill`. Capture `skill_run.run_id` —
     it's referenced by every exit path below.

   Hold onto `session_id` from the JSON — it will be passed to
   `tusk merge` in Step 12 to close the session. **Do not pass it to
   `tusk task-done`; use `tusk merge` for the full finalization
   sequence.**

   > **Early-exit cleanup:** If any step below causes the prompt to
   > stop before reaching the final `retro.md` invocation in Step 12,
   > first call `tusk skill-run cancel <run_id>` to close the open row,
   > then stop. Otherwise the row lingers as `(open)` in
   > `tusk skill-run list` forever.
   >
   > **Pre-start exits don't need cancel.** If
   > `tusk task-start --force --skill tusk` exits 1 (empty backlog) or
   > exits 2 (task not found, already Done, blocked, or missing
   > criteria without `--force`), the skill-run row is never opened, so
   > there is no `run_id` to cancel. Just stop.

1b. **Workflow routing** — If the task's `workflow` field (from the
   `task` object in Step 1) is non-null, the task uses a custom
   workflow. Look up the corresponding prompt file:

   ```
   .codex/prompts/<workflow>.md
   ```

   If the file exists, cancel the tusk skill-run (the handoff prompt
   will open its own run) and **stop following the steps below**,
   following that prompt's instructions instead, passing the task ID
   and `session_id` from Step 1:

   ```bash
   tusk skill-run cancel <run_id>
   ```

   If the file does not exist, log a warning ("Workflow '<workflow>'
   not found — falling back to default development cycle") and
   continue with Step 2 (no cancel — the tusk run stays open for the
   rest of the default flow).

2. **Create a new git branch IMMEDIATELY** (skip if resuming and
   branch already exists):
   ```bash
   tusk branch <id> <brief-description-slug>
   ```
   This detects the default branch (remote HEAD → gh fallback →
   `main`), checks it out, pulls latest, and creates
   `feature/TASK-<id>-<slug>`. It prints the created branch name on
   success.

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
     `files` already exist on disk. Mark all criteria done with
     `--skip-verify` and proceed directly to Step 9 (commit + merge)
     without reimplementing.
   - **`"implement_fresh"`** — no commits and no deliverable files
     found. Proceed normally and implement from scratch.

3. **Determine the best agent** (informational in Codex — there is no
   sub-agent dispatch primitive). Note the task's domain, assignee
   field, and description so the work mirrors the conventions for that
   area.

4. **Confirm failure** — Run the failing tests *before* exploring any
   code when the task is about *fixing* an existing failure. This
   confirms the bug still exists and avoids wasted investigation.

   **When to run this step:**
   - `task_type: bug` → always run.
   - `task_type: test` AND the summary/description indicates fixing a
     failing or flaky test → run.
   - `task_type: test` AND the summary/description indicates *writing
     new tests* (no existing failure to reproduce, e.g. "Add tests for
     X") → **skip this step entirely and proceed to Explore**.
   - All other task types (feature, chore, docs, etc.) → skip.

   1. Check the task description and acceptance criteria for specific
      test commands or test names to run.
   2. If specific tests are named, run them directly. Otherwise, use
      `tusk test-detect` to find the project's test command, then run
      the most relevant subset.
   3. **If tests pass:** the issue may already be fixed or the
      description may be inaccurate — run
      `tusk skill-run cancel <run_id>`, surface this to the user, and
      stop before investigating further.
   4. **If tests fail:** capture the failure output. Use it as the
      primary diagnostic anchor in Step 5.

5. **Explore the codebase before implementing.** Use `Read`, `Grep`,
   `Glob`, and read-only `Bash` to research:
   - What files will need to change?
   - Are there existing patterns to follow?
   - What tests already exist for this area?
   - **For each file you plan to modify**, grep it for keywords related
     to the feature. If a helper function already exists that covers
     what you're about to write, use it instead of duplicating the
     logic.

   Codex has no parallel sub-agent primitive — do the searches inline.
   Report findings before writing any code.

5b. **Scope check — only implement what the task describes.**
   The task's `summary` and `description` define the full scope of
   work for this session. If the description references external
   documents (evaluation docs, design specs, RFCs), treat them as
   **background context only** — do not implement items from those
   docs that go beyond what the task's own description asks for.

6. **Begin implementation.** Codex executes work in the current
   session — there is no delegation to a sub-agent. Apply the patterns
   surfaced in Step 5.

7. **Implement, commit, and mark criteria done.** Work through the
   acceptance criteria from Step 1 as your checklist — **one commit per
   criterion is the default**. For each criterion in order:

   1. Implement the changes that satisfy it.
   2. Commit and mark the criterion done atomically:
      ```bash
      tusk commit <id> "<message>" "<file1>" ["<file2>" ...] --criteria <cid>
      ```
      An alternative `-m` flag form is also supported (useful when
      file paths come first):
      ```bash
      tusk commit <id> "<file1>" ["<file2>" ...] -m "<message>" --criteria <cid>
      ```
      This runs `tusk lint` (advisory — never blocks unless a blocking
      rule fires), stages the listed files, commits with the
      `[TASK-<id>] <message>` format and Co-Authored-By trailer, and
      marks the criterion done — all in one call. The criterion is
      bound to the new commit hash automatically. Duplicate `[TASK-N]`
      prefixes in the message are stripped automatically, and bare
      `--` separators are silently ignored.

      **Always quote file paths** — zsh expands unquoted brackets
      (`[id]`, `[slug]`) as glob patterns before the shell passes
      arguments to `tusk commit`. Any path component containing `[`,
      `]`, `*`, `?`, or spaces must be wrapped in double quotes.

      **Avoid backticks and unescaped `$` in commit messages** — even
      inside double quotes, zsh and bash treat backticks as command
      substitution and `$VAR` / `$(…)` as variable expansion. A
      message that references code (e.g. explaining a
      `flatMap { $0.isEmpty ? nil : $0 } ?? "US"` change) fails with
      `zsh: parse error near '}'` before tusk ever sees the args.
      Drop the backticks (use plain identifiers) or escape every
      metacharacter — double-quoting alone does not protect them.
      This is the same class of zsh-quoting hazard as the file-paths
      note above, just hitting the message argument instead.

      **Grouping criteria:** 2–3 genuinely co-located criteria (e.g.,
      a schema change and its migration) may share one commit — use
      one `--criteria` flag per ID:
      ```bash
      tusk commit <id> "<message>" "<file1>" ["<file2>" ...] --criteria <cid1> --criteria <cid2>
      ```
      Always include a brief rationale in the commit message when
      grouping. **Never** bundle all criteria onto a single
      end-of-task commit.

   **If the task has no git-trackable file changes** (e.g., a venv
   install, a runtime config change, an OS-level operation), skip
   `tusk commit` entirely — it requires at least one file argument
   and will fail with exit code 1 (usage error) if none are provided.
   Mark criteria done directly:
   ```bash
   tusk criteria done <cid> --skip-verify
   ```

   **After each `tusk commit`,** run `git status --short` to confirm
   your files were staged and committed.

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
   git add "<file1>" ["<file2>" ...] && git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: Codex <noreply@anthropic.com>"
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
   git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: Codex <noreply@anthropic.com>"
   ```
   Then mark criteria done with `tusk criteria done <cid> --skip-verify`.

   **If `tusk commit` exits 6 (blocking lint violation)** — the commit
   did NOT land. The violating rule's output is printed verbatim — fix
   it, then retry `tusk commit`. If the violation is a known false
   positive or pre-existing state you can't resolve in this commit,
   bypass with `--skip-lint` (lint only) or widen to `--skip-verify`
   (lint, tests, and pre-commit hooks):
   ```bash
   tusk commit <id> "<message>" "<file>" --skip-lint --criteria <cid>
   ```

   **If `tusk commit` hard-fails because tests fail** (exit code 2 —
   `test_command` is set and returned non-zero), **first verify the
   failure is not pre-existing** before entering the diagnosis loop:

   **Pre-existing failure check:**
   ```bash
   tusk test-precheck
   ```
   Or pass an explicit command when the config-resolved one isn't
   what you want to check against:
   ```bash
   tusk test-precheck --command "<test_command>"
   ```
   `tusk test-precheck` resolves the test command, stashes any local
   changes safely under a uniquely-named entry, runs the test against
   HEAD, and pops that entry by reference. Output is JSON:
   `{pre_existing, exit_code, test_command, stashed}`.

   - **If `pre_existing` is `true`** — the failure is unrelated to
     your changes. Skip the diagnosis loop entirely. Fall back
     immediately to:
     ```bash
     git add <file1> [file2 ...] && git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: Codex <noreply@anthropic.com>"
     ```
     Then mark criteria done with `tusk criteria done <cid> --skip-verify`.

   - **If `pre_existing` is `false`** — your changes introduced the
     failure. Proceed with the diagnosis loop:
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

   3. Log a progress checkpoint:
      ```bash
      tusk progress <id> --next-steps "<what remains to be done>"
      ```
   - All commits should be on the feature branch
     (`feature/TASK-<id>-<slug>`), NOT the default branch.

   The `next_steps` field is critical — write it as if briefing a new
   agent who has zero context. Include what's been done, what
   remains, decisions made, and the branch name.

   **Schema migration reminder:** If the commit adds or modifies a
   migration in `bin/tusk-migrate.py` (or bumps `cmd_init`'s fresh-DB
   `user_version` stamp in `bin/tusk`), run `tusk migrate` on the live
   database immediately after committing.

8. **Review the code locally** before considering the work complete.

9. **Verify all acceptance criteria are done** before pushing:
   ```bash
   tusk criteria list <id>
   ```
   If any criteria are still incomplete, address them now. If a
   criterion was intentionally skipped, note why in the PR description.

10. **Run convention lint (advisory).** `tusk commit` already runs lint
    before each commit. If you need to check lint independently before
    pushing:
    ```bash
    tusk lint
    ```
    Review the output. This check is **advisory only** — non-blocking
    violations are warnings. Fix any clear violations in files you've
    already touched. Do not refactor unrelated code just to satisfy
    lint.

11. **Run review-commits if configured.** Check the review mode first:
    ```bash
    tusk config review
    ```
    - **mode = disabled** (or review key missing): skip review,
      proceed to Step 12.
    - **mode = ai_only**: follow `review-commits.md` end-to-end for
      task `<id>`. After it completes with verdict **APPROVED**,
      proceed to Step 12. If verdict is **CHANGES REMAINING**, run
      `tusk skill-run cancel <run_id>`, surface the unresolved items
      to the user, and stop.

12. **Finalize — merge, push, and run retro.** Execute as a single
    uninterrupted sequence — do NOT pause for user confirmation
    between steps:

    ```bash
    tusk merge <id> --session $SESSION_ID
    ```

    `tusk merge` closes the session, merges the feature branch into
    the default branch, pushes, deletes the feature branch, and marks
    the task Done. It returns JSON including an `unblocked_tasks`
    array. If there are newly unblocked tasks, note them in the retro.

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
    `No branch found matching feature/TASK-<id>-*` and you are NOT on
    the default branch, switch to the default branch first
    (`git checkout <default_branch>`), then retry `tusk merge <id> --session <session_id>`.

    **PR mode:** If the project uses PR-based merges
    (`merge.mode = pr` in config, or when passing `--pr`), use:
    ```bash
    tusk merge <id> --session $SESSION_ID --pr --pr-number <N>
    ```
    This squash-merges via `gh pr merge` instead of a local
    fast-forward.

    **No-commit closure (`wont_do` / `duplicate`):** If the task
    should be closed *without* shipping any code — an
    evaluation/spike whose answer is "don't do it", or a task that
    turns out to be a duplicate — use `tusk abandon` instead of
    `tusk merge`:
    ```bash
    tusk abandon <id> --reason wont_do|duplicate --session $SESSION_ID [--note "<rationale>"]
    ```
    `tusk abandon` switches off the feature branch, deletes it
    (force), closes the session, and marks the task Done with the
    given `closed_reason` in one call. **Refuses** if the feature
    branch has commits not on the default branch — in that case use
    `tusk merge` to ship the work, or delete the branch manually if
    you really want to discard it. The optional `--note` records the
    decision rationale on `task_progress` so the audit trail
    survives. After `tusk abandon` exits 0, run `retro.md` exactly as
    you would after `tusk merge`.

    After `tusk merge` (or `tusk abandon`) exits 0, close out the
    tusk skill-run so its cost is captured before retro starts its
    own run:
    ```bash
    tusk skill-run finish <run_id>
    ```

    Then emit the canonical end-of-run summary before handing off to
    retro:
    ```bash
    tusk task-summary <id> --format markdown
    ```

    This prints a single markdown block with the task identity,
    closed reason, total cost, wall/active duration, diff stats
    (files changed, lines added/removed, commit count), criteria
    counts, review pass count, and reopen count. Show it verbatim to
    the user — do not re-render or summarize it. Diff stats are
    filtered to commits that reference `[TASK-<id>]`.

    Then follow `retro.md` immediately — do not ask "shall I run
    retro?". The retro prompt expects the canonical task-summary
    block to have already been printed and intentionally does not
    re-emit it.

## Other Subcommands

If the user invoked a subcommand (e.g., `/tusk done`, `/tusk list`,
`/tusk blocked`):

| Argument | Action |
|----------|--------|
| (none) | Get next ready task and automatically start working on it |
| `<id>` | Begin full workflow on task `<id>` |
| `list <n>` | Show top N ready tasks |
| `done <id>` | `tusk task-done <id> --reason completed` (manual; never use this when a feature branch exists — use `tusk merge`) |
| `view <id>` | `tusk task-get <id>` |
| `domain <value>` | `tusk task-select --domain <value>` then begin work |
| `assignee <value>` | `tusk task-select --assignee <value>` then begin work |
| `blocked` | `tusk deps blocked` |
| `wip` | `tusk -header -column "SELECT id, summary, priority, domain, assignee FROM tasks WHERE status = 'In Progress'"` |
| `preview` | `tusk -header -column "SELECT id, summary, priority, complexity, domain, assignee, description FROM v_ready_tasks ORDER BY priority_score DESC, id LIMIT 1"` |

For `list <n>`:
```bash
tusk -header -column "
SELECT id, summary, priority, complexity, domain, assignee
FROM v_ready_tasks
ORDER BY priority_score DESC, id
LIMIT <n>;
"
```
