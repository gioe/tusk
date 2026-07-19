---
name: tusk
description: Get the most important task that is ready to be worked on
allowed-tools: Bash, Task, Read, Edit, Write, Grep, Glob
---

# Tusk Skill

The primary interface for working with tasks from the project task database (via `tusk` CLI). Use this to get the next task, start working on it, and manage the full development workflow.

> Use `/create-task` for task creation — handles decomposition, deduplication, criteria, and deps. Use `tusk task-insert` only for bulk/automated inserts.

## Setup: Upgrade and Reload

Before any task workflow command, run:

```bash
tusk upgrade --no-commit
```

After the command finishes, immediately read the current
`.claude/skills/tusk/SKILL.md` from disk exactly once and restart this
`/tusk` workflow from that freshly loaded file, preserving the user's
original task argument or no-argument intent. This applies whether the
command reports `Upgrade complete` or `Already up to date`; do not
continue from the stale skill text already loaded into this session.
After that one reload, do not repeat this upgrade/reload bootstrap
again for the same `/tusk` invocation. If the command reports that
this is the tusk source repo and `git pull` is the update path,
continue normally with the already loaded instructions.

## Setup: Discover Project Config

Before any operation that needs domain or agent values, run:

```bash
tusk config
```

This returns the full config as JSON (domains, agents, task_types, priorities, complexity, etc.). Use the returned values (not hardcoded ones) when validating or inserting tasks.

## Commands

### Get Next Task (default - no arguments)

Finds the highest-priority task that is ready to work on (no incomplete dependencies), opens a session for it, flips its status to In Progress, opens a skill-run row for cost tracking, and returns the same JSON blob documented under "Begin Work on a Task" below — all in one call.

```bash
tusk task-start --force --skill tusk
```

The `--force` flag bypasses the **zero-criteria** guard only (emits a warning rather than hard-failing). It does **not** bypass dep blocking or unresolved external blockers — those are separate guards. To bypass an unmet `blocks`-type dependency, pass `--force-deps`; to bypass an open `contingent` dependency, pass `--force-contingent` (use both sparingly — dependency guards exist for a reason). The `--skill tusk` flag opens a `skill_runs` row attributed to this task; `run_id` is returned under `skill_run.run_id` in the JSON — capture it for the cancel/finish calls later.

**Empty backlog**: If the command exits with code 1, the backlog has no ready tasks. Check why:

```bash
tusk -header -column "SELECT status, COUNT(*) as count FROM tasks GROUP BY status"
```

- If there are **no tasks at all** (or all are Done): inform the user the backlog is empty and suggest running `/create-task` to add new work.
- If there are **To Do tasks but all are blocked**: inform the user and suggest running `/tusk blocked` to see what's holding them up.
- If there are **In Progress tasks**: inform the user and suggest running `/tusk wip` to check on active work.

Do **not** suggest `/groom-backlog` or `/retro` when there are no ready tasks — those skills require an active backlog or session history to be useful.

On success, the JSON blob's `task.id` is the task you just started and `skill_run.run_id` is the open skill-run row. **Immediately proceed to step 1b of the "Begin Work on a Task" workflow** — do not wait for additional user confirmation.

Before proceeding to Step 1b, state the resolved task identity verbatim: `Working on TASK-<id>: <summary>`. Treat the JSON blob's `task.id` as the single source of truth; never type a task ID that did not come from this output. This gives the operator one chance to correct a misread or hallucinated ID before any downstream command runs.

### Begin Work on a Task (with task ID argument)

When called with a task ID (e.g., `/tusk 6`), begin the full development workflow. When called with no argument, the "Get Next Task" step above has already run `tusk task-start --force --skill tusk` for you — **skip Step 1 entirely and pick up at Step 1b (context hydration)**, using the JSON blob and the `skill_run.run_id` you already captured.

**Follow these steps IN ORDER:**

1. **Start the task and begin cost tracking** — fetch details, check progress, create/reuse session, set status, and open the skill-run row in one call:
   ```bash
   tusk task-start <id> --force --skill tusk
   ```
   The `--force` flag bypasses the **zero-criteria** guard only (emits a warning rather than hard-failing) — it does **not** bypass dep blocking or unresolved external blockers. If the task has unmet `blocks`-type dependencies, the call exits 2 with the blocker list; pass `--force-deps` to bypass that guard with a warning. If the task has open `contingent` dependencies, the call exits 2 with the upstream list; pass `--force-contingent` to bypass that guard with a warning. Use both dependency bypasses sparingly. The `--skill tusk` flag opens a `skill_runs` row so this session's spend can be attributed to the task. This returns a JSON blob with these keys:
   - `task` — full task row (summary, description, priority, domain, assignee, etc.)
   - `progress` — array of prior progress checkpoints (most recent first). If non-empty, the first entry's `next_steps` tells you exactly where to pick up. Skip steps you've already completed (a task workspace may already be recorded, some commits may already be made). Use `git log --oneline` in the task workspace to see what's already been done.
   - `criteria` — array of acceptance criteria objects (id, criterion, source, is_completed, criterion_type, verification_spec). These are the implementation checklist. Work through them in order during implementation. Mark each criterion done (`tusk criteria done <cid>`) as you complete it — do not defer this to the end. Non-manual criteria (type: code, test, file) run automated verification on `done`; use `--skip-verify` if needed. If the array is empty, proceed normally using the description as scope.
   - `session_id` — the session ID to use for the duration of the workflow (reuses an open session if one exists, otherwise creates a new one)
   - `criteria_already_passing` — count of incomplete, non-deferred code/file-type criteria whose verification specs already pass on the current checkout (issue #1051). If > 0, print `N/M criteria already pass — possible convergent completion` (N = this count, M = incomplete criteria) **before any implementation work begins** — sibling work may have already shipped this task's deliverables. `deliverable_check_needed` is forced `true` in this case, so Step 2's `tusk check-deliverables` run will classify the disk state (`mark_done` / `merged_not_closed` / etc.) before you write any code.
   - `skill_run` — `{run_id, skill_name, started_at, task_id}` for the skill-run row opened by `--skill`. Capture `skill_run.run_id` — it's referenced by every exit path below.

   Before proceeding to Step 1b, state the resolved task identity verbatim: `Working on TASK-<id>: <summary>`. Treat the JSON blob's `task.id` as the single source of truth; never type a task ID that did not come from this output. This gives the operator one chance to correct a misread or hallucinated ID before any downstream command runs.

   Hold onto `session_id` from the JSON — it will be passed to `tusk merge` in step 12 to close the session. **Do not pass it to `tusk task-done`; use `tusk merge` for the full finalization sequence.**

   > **Early-exit cleanup:** If any step below causes the skill to stop before reaching the final `/retro` invocation in Step 12, first call `tusk skill-run cancel <run_id>` to close the open row, then stop. Otherwise the row lingers as `(open)` in `tusk skill-run list` forever. The explicit cancel calls below cover the known post-start early-exit paths; if you hit an unexpected bail-out, cancel before returning.
   >
   > **Pre-start exits don't need cancel.** If `tusk task-start --force --skill tusk` exits 1 (empty backlog — "No ready tasks found") or exits 2 (task not found, already Done, already has an active session without `--force-session`, has unmet `blocks`-deps without `--force-deps`, has open `contingent` deps without `--force-contingent`, has open external blockers, or missing criteria without `--force`), the skill-run row is never opened, so there is no `run_id` to cancel. Just stop.
   >
   > **Declining a just-started task (skip path):** If the task should not be worked after all (the operator declines the auto-surfaced task, or the premise turns out to be wrong) and no implementation work has landed yet — no progress checkpoints, no `[TASK-<id>]` commits — revert it to To Do instead of leaving it In Progress:
   > ```bash
   > tusk skill-run cancel <run_id>
   > tusk task-unstart <id> --force --close-sessions
   > ```
   > `--close-sessions` closes the open session that `task-start` created instead of refusing on it (issue #1043). It does NOT bypass the progress-checkpoint or commit-overlap guards — if those refuse, the task has real work attached: finish it, or close it explicitly via `tusk abandon`.

1b. **Hydrate task context before routing or exploring** — after `tusk task-start` succeeds, read the compiled brief before code exploration:
   ```bash
   tusk task-brief <id>
   ```
   Treat the compiled brief as the task's durable context packet. It contains the same task identity and criteria from `task-start`, plus scope, dependencies, objectives, task context items, verification specs, and `context_health_warnings`.

   Use the brief to make these decisions before Step 1c or any code-reading pass:
   - **Classify the task mode** — choose the operating mode from the task summary, description, type, criteria, and verification specs: bug fix, feature, test-only, docs-only, investigation/spike, DB-only/no-code, or workflow handoff. This classification controls whether Step 4's confirm-failure rule applies and how much exploration is needed.
   - **Treat incomplete criteria as the execution plan** — filter the brief's acceptance criteria to incomplete, non-deferred rows. Those incomplete criteria are the execution plan. Work them in order unless a later criterion is a prerequisite for an earlier one; if you reorder them, state why. Do not invent a broader plan when the criteria already define the deliverable.
   - **Treat scope as a contract** — scope is a contract: compare planned edits against the brief's `scope` rows before implementation. If a needed path is missing, add scope with a concrete reason before editing or committing. Do not treat mentioned background docs, adjacent helpers, or convenient cleanup as authorized scope unless they are in the scope table or you explicitly add them.
   - **Validate context health** — read every `context_health_warnings` entry. Missing scope paths, stale verification specs, absent entry points, conflicting assumptions, or dependency warnings must be resolved, incorporated into the plan, or surfaced before implementation.
   - **Gate on blocking open questions** — inspect `context.open_questions`, assumptions, risks, and decisions. Do not begin implementation while blocking open questions remain. A blocking question is one whose answer can change the files to edit, the acceptance criteria interpretation, the task mode, or whether the task should proceed at all. Ask the operator or log a progress checkpoint and stop rather than guessing.

   If the compiled brief contradicts the `task-start` JSON, trust the compiled brief for planning and rerun `tusk task-get <id>` only to diagnose the mismatch. Keep `session_id` and `skill_run.run_id` from `task-start`; `task-brief` is read-only and does not replace them.

1c. **Workflow routing** — If the task's `workflow` field (from the `task` object in step 1) is non-null, the task uses a custom workflow instead of the default development cycle. Look up the corresponding skill:
   ```
   Read file: .claude/skills/<workflow>/SKILL.md
   ```
   If the file exists, cancel the /tusk skill-run (the handoff skill will open its own run) and **stop following the steps below**, following that skill's instructions instead, passing the task ID and session_id from step 1:
   ```bash
   tusk skill-run cancel <run_id>
   ```
   If the file does not exist, log a warning ("Workflow '<workflow>' not found — falling back to default development cycle") and continue with step 2 (no cancel — the /tusk run stays open for the rest of the default flow).

2. **Create or reuse the task-owned workspace IMMEDIATELY**:
   Before changing into the task worktree, capture the current stable checkout and a stable `tusk` binary for post-merge finalization:
   ```bash
   TUSK_PRIMARY_CWD=$(pwd)
   TUSK_PRIMARY_BIN=$(command -v tusk)
   ```
   Keep these variables for Step 12. `tusk merge` and `tusk abandon` may remove the task worktree before the final `skill-run finish`, `task-summary`, and `/retro` handoff run, so those commands must be launched from a checkout that still exists after cleanup.

   **Writable-root preflight (before the first create):** If `TUSK_WORKTREE_ROOT` is explicitly set, preserve it and use the normal command below without adding `--workspace-root`. Otherwise, when the active runtime declares authorized writable filesystem roots, compare the expanded default `~/.tusk/worktrees` against those roots before creating anything. Do not rely on `test -w` or Unix permissions alone: a managed sandbox can deny an OS-writable path.

   If the default is inside an authorized writable root, use the normal command unchanged. If it is outside every authorized root, choose an environment-declared writable root outside the primary checkout (prefer the runtime's temporary/external workspace root), derive the pool `<authorized-root>/tusk-worktrees`, and use the fallback command:
   ```bash
   tusk task-worktree create <id> <brief-description-slug> --workspace-root <authorized-root>/tusk-worktrees
   ```
   Never hardcode `/private/tmp` or another platform-specific path, never create an inaccessible worktree and then relocate it, and never put the pool inside the primary checkout (which would dirty or nest it). If the runtime exposes writable-root metadata but no suitable authorized root outside the checkout, stop before creation and request a writable root. If the runtime exposes no writable-root metadata, preserve the existing behavior and use the normal command. The CLI adds the per-repository namespace beneath the selected pool, preserving collision protection; do not append the repository name yourself.

   ```bash
   tusk task-worktree create <id> <brief-description-slug>
   ```
   This creates a recorded task workspace and feature branch, or returns the existing recorded workspace for the task. Parse the JSON response, then `cd` into `workspace_path` before exploring, editing, testing, committing, or merging. If `created` is `false`, continue from that existing workspace; do not create another branch or overlapping worktree. If you are already in the returned `workspace_path`, stay there.

   **CLI behavior testing from a worktree — invoke `$workspace_path/bin/tusk`, not `tusk`.** When validating the live behavior of a `bin/tusk-*.py` change you made inside the worktree (only relevant for tusk source-repo tasks), the `tusk` wrapper on `$PATH` resolves to the **primary checkout's** `bin/tusk` — its `$SCRIPT_DIR/tusk-*.py` dispatch then runs the primary's Python helpers regardless of CWD, so your worktree-local edits are silently ignored. The CLI exits 0 with stale-but-plausible output, which is the symptom — **silently stale behavior** masquerading as a passing live check. Unit tests don't have this problem because they resolve `SCRIPT = os.path.join(REPO_ROOT, 'bin', '...')` relative to the test file's own path and naturally pick up the worktree's modules. To exercise the worktree's helpers from the CLI, invoke the worktree's wrapper explicitly: `$workspace_path/bin/tusk <subcommand>` (or `./bin/tusk <subcommand>` when already `cd`'d into the worktree). Originally surfaced as issue #860 during TASK-436.

   **Symlinked Python virtualenvs can also import primary-checkout code.** This is separate from the `bin/tusk` wrapper caveat above. When `worktree.symlink_files` links a primary checkout virtualenv into a task worktree, that venv may contain editable-install metadata or `.pth` files pointing at the primary checkout's source tree. Running the symlinked Python, `make run-script`, or the `tusk commit` pytest gate can then import and test primary-checkout modules while your task worktree source edits are invisible. For scraper worktrees, set `PYTHONPATH=$workspace_path/apps/scraper/src` before smoke commands or commit-gate runs that must import worktree source, for example `PYTHONPATH=$workspace_path/apps/scraper/src make run-script ...` or `PYTHONPATH=$workspace_path/apps/scraper/src ./bin/tusk commit ...`. If a project has a different source root, use that worktree-local `src` path instead.

   If you need to inspect recorded workspaces before deciding where to continue, run:
   ```bash
   tusk task-worktree list --format json
   ```
   Use the row for this task when present. The recorded workspace is the normal task boundary; do not use `tusk branch` for the default `/tusk` workflow.

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

3. **Determine the best subagent(s)** based on:
   - Task domain
   - Task assignee field (often indicates the right agent type)
   - Task description and requirements

4. **Confirm failure using relevant evidence** — Before exploring code for a task that fixes an existing failure, confirm the reported failure using the evidence type that actually reproduces it. Tests are authoritative only when they exercise the reported behavior; the mere presence of a focused test does not make it the reproducer.

   **When to run this step:**
   - `task_type: bug` → always confirm the failure. For a visual or screenshot bug, use a current screenshot or manual visual check against the active build/checkout. For a logic-test-backed bug, run the relevant failing test.
   - `task_type: test` AND the summary/description indicates fixing a failing or flaky test → run
   - `task_type: test` AND the summary/description indicates *writing new tests* (no existing failure to reproduce, e.g. "Add tests for X", "Write test suite for Y") → **skip this step entirely and proceed to Explore**
   - All other task types (feature, chore, docs, etc.) → skip

   1. Identify the evidence claimed to reproduce the failure in the task description and acceptance criteria: a test command, current screenshot, or manual observation.
   2. **Visual or screenshot evidence:** inspect the current screenshot or perform the stated manual visual check against the active build/checkout. If the defect is visible, treat that as the confirmed reproduction and use it as the primary diagnostic anchor in step 5 (Explore). A passing logic test that does not assert rendering must not cancel the run. Only a test that directly asserts the reported rendering defect, such as a screenshot, golden, pixel, or rendering assertion, can invalidate that visual evidence.
   3. **Logic-test evidence:** if specific relevant tests are named, run them directly. Otherwise, use `tusk test-detect` to find the project's test command, then run the most relevant subset.
   4. **If a relevant reproducer test passes**: before concluding the issue is already fixed, inspect the recorded failure evidence for time/date sensitivity. Signals include date- or year-named tests, local-date versus UTC assertions, and failure timestamps clustered in a narrow wall-clock window (use `run_started_at` / `run_ended_at` from a recorded `tusk test-precheck` verdict when available). When those signals exist, retry under a controlled timezone such as `TZ=UTC` and, when the test framework supports it, a frozen clock at the recorded failure instant. If the controlled reproduction fails, continue to Explore using that output. Only when no time-sensitive signal exists, or controlled retries still pass, run `tusk skill-run cancel <run_id>`, surface that the issue may already be fixed or inaccurate, and stop before investigating further.
   5. **If a relevant reproducer test fails**: capture the failure output. Use it as the primary diagnostic anchor in step 5 (Explore).

   **State-mutating reproductions:** If the failing command writes to tracked files (e.g. `tusk version-bump`, `tusk changelog-add`, `tusk commit`, `tusk merge --rebase`), do **not** reproduce it against the active task worktree — the writes dirty the working tree and may block `tusk merge` / `tusk abandon` later. Reproduce against a throwaway location instead: `cd` into a fresh `tmp_path` repo (the integration-test pattern) and run the command there, or `git stash` the result immediately after. This is the filesystem analogue of the user-memory guidance to use a `TUSK_DB` throwaway for state-mutating DB reproductions.

5. **Explore the codebase before implementing** — use a sub-agent to research:
   - What files will need to change?
   - Are there existing patterns to follow?
   - What tests already exist for this area?
   - **For each file you plan to modify**, grep it for keywords related to the feature (e.g., the concept name, the config key, the resource type). If a helper function already exists that covers what you're about to write, use it instead of duplicating the logic.

   Report findings before writing any code.

5b. **Declare scope before the first commit.** The commit-time scope guard reads from the authoritative `task_scope` table (TASK-471). It falls back to the `task_referenced_paths` hint cache only for legacy `scope_enforced=0` tasks; for current `scope_enforced=1` tasks, no `task_scope` rows means the commit is rejected as missing scope declaration. Before staging the first commit, run `tusk scope list <id>` to see what the table currently authorizes:

   - **If the list already covers the files you plan to touch**, proceed to commit; no action needed. Migration 73 backfilled `auto_derived` rows from your description and acceptance criteria; tasks created with `tusk task-insert --scope/--creates` have `operator_declared`/`creates` rows from the start.
   - **If you are declaring missing paths before any task work has landed**, run `tusk scope add <id> <path> --reason "<why>"` for each one before staging. The implicit source is `operator_declared` until the task has a progress checkpoint or committed criterion, so up-front declarations do not look like mid-task drift in retro.
   - **If exploration revealed extra paths after task work has landed** (a helper you have to extend, a sibling test file, an unrelated config that has to change), run `tusk scope add <id> <path> --reason "<why>"` for each one before staging. The implicit source is then `expanded_mid_task`, and the rationale lets retro answer "why did scope grow mid-task?" without guessing.
   - **If `tusk scope list` is empty on a `scope_enforced=1` task**, declare the files you plan to edit before staging. Empty scope is not a vacuous pass for current tasks; it is a metadata gap that the guard rejects before commit.
   - **If the task is a legitimately repo-wide refactor** (e.g. a rename across every skill or every Python file), it should have been created with `tusk task-insert --unbounded`. If it wasn't, you can stamp it now: `tusk scope add <id> "**" --source operator_declared --reason "..."` is a partial workaround, but the long-term fix is to recreate the task with `--unbounded` so the guard silently passes any staged file. On tasks that already have unbounded `**` scope, redundant `tusk scope add` calls no-op with a note instead of adding dead rows.

   Externally referenced design docs (e.g. a `docs/PILLARS.md` link in the description) are background context, not scope — do not add them via `scope add` unless you actually plan to edit them.

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

       **Avoid backticks and unescaped `$` in commit messages — `tusk commit` enforces this at the boundary (issue #881).** `tusk commit` runs `_validate_message_metacharacters` after the empty-message check and before any git/sqlite subprocess; the call exits 1 with a diagnostic naming the metacharacter class, byte offset, and repr-quoted message when the message contains a backtick, `$(...)`, `${...}`, or bare `$<identifier>`. The guard exists because zsh and bash expand those patterns BEFORE tusk sees the argv, even inside double quotes — TASK-464 shipped a JSON blob into commit 984ca1a on origin/main when a literal backticked `tusk sync-main` inside a double-quoted message got executed by zsh. The guard rejects rather than auto-escaping so the agent rewrites the message; auto-escape would silently mutate the intent. Diagnostic recommends plain identifiers (drop the backticks) or wrapping the entire message in single quotes when the literal character must appear. The same guard (the shared `reject_shell_metacharacters` helper in `bin/tusk-git-helpers.py`) now also covers `tusk task-insert`, `tusk task-update`, and `tusk criteria add` text args (issue #1106): summary, inline description, and criterion text reject the same metacharacters before any DB write. Issue #1107 extended it to the remaining tusk-owned text-arg surfaces: `tusk progress` (`--note`, `--next-steps`), `tusk context add` (`--content`), `tusk jot` (the note arg), and `tusk review` (`add-comment` comment text plus the `--note` on `resolve`/`approve`/`request-changes`) all reject the same metacharacters before any DB write. Issue #1108 closed the last agent-relayed sibling gap — `tusk jot`'s **category** positional is now guarded too — and audited the operator-authored DB-write surfaces (`tusk conventions add`/`update`, `tusk glossary set-definition`/`add`, `tusk lint-rule add`/`update` message), which are intentionally **exempt** (documented, not guarded): they are operator-authored, low-frequency, and legitimately contain literal shell-syntax examples (they document shell hazards), so guarding would block their primary use case and there is no agent-relay corruption vector. `task-insert`'s `--description-file` reads the file directly and is the immune path for untrusted text; typed-criteria and file-type verification specs are NOT checked (shell code by design). The same class of hazard still exists for `gh issue close --comment` (`/address-issue` Step 9) and the `gh issue comment`/`gh pr comment` calls in `/review-commits`, but **`gh` is an external tool tusk does not wrap, so those surfaces are NOT covered by any guard** — manual care still required there.

       **Grouping criteria:** 2–3 genuinely co-located criteria (e.g., a schema change and its migration) may share one commit — use one `--criteria` flag per ID:
       ```bash
       tusk commit <id> "<message>" "<file1>" ["<file2>" ...] --criteria <cid1> --criteria <cid2>
       ```
       Always include a brief rationale in the commit message when grouping. **Never** bundle all criteria onto a single end-of-task commit. Exception: if several criteria all land in one new file or one inseparable file-local change, bundle them in one commit with an explicit rationale instead of truncating/restoring the file just to simulate separate commits.

    **If a criterion does not apply to the implementation path you chose** (e.g., a mutually-exclusive "do X OR document why exempt" pair where you did X), use `tusk criteria skip` — NOT `tusk criteria done --skip-verify`:
    ```bash
    tusk criteria skip <cid> --reason "not applicable: chose <chosen-branch> over <skipped-branch>"
    ```
    `done --skip-verify` stamps the criterion with HEAD's commit hash, leaking an unrelated commit into the audit trail and triggering "shares commit" warnings between unrelated criteria. `skip` sets `is_deferred=1` with the rationale recorded in `deferred_reason`; the `task-done` gate and `v_criteria_coverage` view exclude deferred criteria automatically, so the task closes cleanly. Reserve `done --skip-verify` for criteria that ARE satisfied but cannot be auto-verified (the cases below).

    **If the task has no git-trackable file changes** (e.g., a venv install, a runtime config change, an OS-level operation, or a DB-only deliverable like `tusk conventions update` / `tusk lint-rule add`), skip `tusk commit` entirely — it requires at least one file argument and will fail with exit code 1 (usage error) if none are provided. Mark criteria done directly:
    ```bash
    tusk criteria done <cid> --skip-verify
    ```
    Once every criterion is marked done, the feature branch will have no `[TASK-<id>]` commits to merge — close out via Step 12's `tusk abandon <id> --reason completed --note "<rationale>"` path rather than `tusk merge` (which refuses on an empty branch).

    **If a criterion requires filing follow-up tasks** (typical for investigation/triage tasks whose criteria read "file focused follow-up tasks covering each distinct break"), do NOT call `tusk task-insert` directly. Dupe-check first so a freshly-filed sibling task isn't immediately superseded by an existing one:
    ```bash
    tusk dupes check "<proposed summary>"
    ```
    If the check returns a match, amend the existing task (e.g., `tusk criteria add <id> "<criterion>"` or `tusk task-update <id>`) instead of creating a new one. If no match is found, prefer `/create-task` over a raw `tusk task-insert` — `/create-task` runs the same dedup check, decomposes scope, and applies the project's task conventions in one call. Use `tusk task-insert` only when scripting bulk inserts where the dedup step has already been done.

    **After each `tusk commit` in foreground mode**, run `git status --short` to confirm your files were staged and committed — a zero-exit commit that produced no diff (e.g. all files were already tracked with no changes) will silently succeed without staging anything.

    **If `tusk commit` exits 9 (concurrent commit active)**, another invocation holds the operation lock for the same worktree and this process did not run `git commit`. Wait for the active invocation to finish and inspect its `TUSK_COMMIT_RESULT`. Retry only when that result shows the requested commit did not land. If the result is unavailable, inspect HEAD with `git log -1 --format='%H %s'`, inspect the selected paths with `git status --short -- "<file1>" ["<file2>" ...]`, and inspect criterion bindings with `tusk criteria list <id>` before deciding. If the requested TASK commit and intended criterion bindings landed and the selected requested changes are clean, do not reissue `tusk commit`; if the evidence is inconsistent, investigate instead of retrying blindly. Do not interpret exit 9 as a Git failure or mark criteria directly from the losing invocation.

    **If `tusk commit` fails with `pathspec did not match any files`** (exit code 3, git-add error), first check whether the file was already committed in a prior `tusk commit` call for this task (e.g., when all changes go into a single file committed with earlier criteria), or whether the file was removed via `git rm` (which stages the deletion — `tusk commit` then can't find the path to re-add). In either case, `git add && git commit` would also fail — just mark the remaining criteria done directly:
    ```bash
    tusk criteria done <cid> --skip-verify
    ```
    If the error is a genuine pathspec mismatch (not an already-committed file), always pass file paths relative to the repo root (e.g., `ios/SomeFile.swift`, not `SomeFile.swift` from inside `ios/`). If the error persists, fall back to a path-limited commit:
    ```bash
    git add -- "<file1>" ["<file2>" ...]
    git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>" -o -- "<file1>" ["<file2>" ...]
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
    git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
    ```
    Then mark criteria done with `tusk criteria done <cid> --skip-verify`.

    **If `tusk commit` exits 6 (blocking lint violation)** — the commit did NOT land. A non-advisory lint rule fired (Rule 1 raw sqlite3, Rule 3 hardcoded DB path, Rule 11 bad SKILL.md frontmatter, Rule 16 DB-backed blocking rules, Rules 18/19 MANIFEST drift, Rule 21 multi-trailing-newlines, etc.). The violating rule's output is printed verbatim — fix it, then retry `tusk commit`. Advisory-only rules (Rule 13 VERSION bump missing, Rule 15 big-bang commits, Rule 17 DB-backed advisory, etc.) still print WARN lines but do NOT exit non-zero and do NOT block. If the violation is a known false positive or pre-existing state you can't resolve in this commit, bypass with `--skip-lint` (lint only) or widen to `--skip-verify` (lint, tests, and pre-commit hooks):
    ```bash
    tusk commit <id> "<message>" "<file>" --skip-lint --criteria <cid>
    ```
    Lint output during commit is now filtered: only rules with violations print — passing rules are suppressed. If the last lint pass was clean, you won't see any lint output at all.

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
    `tusk test-precheck` resolves the test command from `--command`, then `config.test_command`, then `tusk test-detect`. When the working tree is dirty it stashes local changes under a *uniquely-named* entry, runs the test against HEAD, and pops *that entry by reference* — never by top-of-stack. When the working tree is clean it runs the test directly without touching `git stash` at all. Output is JSON on stdout: `{pre_existing, exit_code, test_command, stashed, diverged_from_default, diverged_paths}`, plus `{flake_runs_total, flake_failures, flaky_suspect}` when `--flake-retries N` (N>0) was passed; the test command's own output is redirected to stderr so programmatic callers can `json.loads(stdout)` directly. Do **not** fall back to the raw `git stash && … ; git stash pop` snippet — when the tree is clean, the empty `git stash` becomes a no-op and `git stash pop` will pop a stale foreign entry and silently trash unrelated state. If precheck exits non-zero, it prints a recovery message on stderr (always including the stash message, when one was created) so you can finish the pop manually; it never silently falls through with changes orphaned in the stash list.

    Branch on the verdict **in this order** — `flaky_suspect` first, then divergence, then the `pre_existing` true/false split:

    - **If `flaky_suspect` is `true`** — the N+1 HEAD runs disagreed on identical code, so the test is **flaky, not a regression you introduced** (issue #1076). Do **not** enter the diagnosis loop and do **not** conclude the failure is pre-existing. Simply **retry the same `tusk commit`** with the same arguments — the gate re-runs the test and a flake will usually pass on the next attempt. Retry up to 3 times; if it still fails *and* `flaky_suspect` stops appearing, fall through to the branches below. If it keeps flapping, log a progress note naming the flaky test and surface it to the user rather than force-committing.

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
      git commit -m "[TASK-<id>] <message>" --trailer "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>" -o -- "<file1>" ["<file2>" ...]
      ```
      Then mark criteria done with `tusk criteria done <cid> --skip-verify`. The `-o -- <files>` form is required here too; a plain `git commit` would include any unrelated paths that were staged before this task.

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

    **Post-merge verification criteria:** If a criterion can only be verified after the change lands on the default branch (for example, a `workflow_dispatch` run, production deploy check, or external system callback), do not leave it open for `tusk merge` to close implicitly. Defer it explicitly before Step 12:
    ```bash
    tusk criteria skip <criterion_id> --reason "post-merge verification: <what will be checked after TASK-<id> lands>"
    ```
    Capture the exact post-merge check in the reason. `tusk merge` refuses ordinary open, non-deferred criteria so a task is not marked Done just because finalization used `task-done --force`.

    **Recording the outcome after close (issue #1058):** once the deferred check is actually performed (e.g. the push-triggered CI run on the default branch goes green), record it with `tusk criteria done <criterion_id>` — this works even after the task is Done, clears `is_deferred` while keeping `deferred_reason` for history, and emits `deferral_cleared` in the JSON so the audit trail distinguishes "verified post-merge" from "never performed". Do not leave the criterion permanently deferred once the verification has happened.

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

12. **Finalize — merge, push, and run retro.** Execute as a sequence — run each command in its own tool call and read its result before issuing the next, but do NOT pause for user confirmation between steps:
    ```bash
    tusk merge <id> --session $SESSION_ID
    ```
    `tusk merge` closes the session, merges the feature branch into the default branch, pushes, deletes the feature branch, and marks the task Done. It returns JSON including an `unblocked_tasks` array. If there are newly unblocked tasks, note them in the retro.

    The merge path runs a pre-merge lint gate by default. If that gate blocks on a known false positive or pre-existing issue, use `--skip-lint` to skip only lint. Use `--skip-verify` only when you need the broader bypass; for TASK-586 / GitHub issue #996 it currently skips lint as well, and it is reserved to skip future pre-merge verification gates as they are added.

    `tusk merge` refuses to proceed while ordinary non-deferred criteria are still open. Complete them, or use Step 9's explicit post-merge verification deferral pattern when the check is impossible before merge.

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

    **Partial-cleanup exit code 3 (TASK-504):** If `tusk merge` exits **3**, the no-checkout fast-forward push, session-close, and task-done all succeeded — the task is Done and the work is on `origin/<default>` — but the local worktree directory and/or feature branch could not be removed (typically an untracked file outside the auto-symlink set blocked `git worktree remove`). The stderr message names the leftover artifact. Treat exit 3 like exit 0 for workflow purposes: still return to the stable checkout and run `skill-run finish`, `task-summary`, and `/retro` as described below. Clean up the leftover worktree manually (`git worktree remove --force <path>` and `git branch -D <feature-branch>`) after the retro, or surface it to the user.

    **Sibling-worktree DB fallback:** If the default branch is checked out in a sibling worktree and the primary checkout is unusable, run the merge from the sibling worktree while pinning tusk to the primary repo's DB:
    ```bash
    TUSK_PROJECT=<primary_repo_path> tusk merge <id> --session $SESSION_ID --rebase
    ```
    This is the correct fallback when running `tusk merge` from the sibling worktree fails with `no such table: task_sessions`: that worktree has the git state needed for the merge, but tusk resolved its database relative to the sibling CWD. `TUSK_PROJECT` keeps tusk pointed at the primary repo's project database while git commands operate in the current worktree.

    **PR mode:** If the project uses PR-based merges (`merge.mode = pr` in config, or when passing `--pr`), use:
    ```bash
    tusk merge <id> --session $SESSION_ID --pr --pr-number <N>
    ```
    This squash-merges via `gh pr merge` instead of a local fast-forward.

    **No-commit closure (`wont_do` / `duplicate` / `completed`):** If the task should be closed *without* shipping any code, use `tusk abandon` instead of `tusk merge`:
    ```bash
    tusk abandon <id> --reason wont_do|duplicate|completed --session $SESSION_ID [--note "<rationale>"]
    ```
    Three reason values are accepted:
    - **`wont_do`** — an evaluation/spike whose answer is "don't do it".
    - **`duplicate`** — the task turns out to overlap an already-tracked one. If the already-tracked task is an **In Progress duplicate**, do not start a fresh `/tusk <id>` on that task; route to `/resume-task <id>` or reuse its existing open session and skill-run so the prior skill-run is not orphaned.
    - **`completed`** — the goal was met but no `[TASK-N]` commits land on the default branch. Three sub-cases:
        - *convergent-completion* (issue #580): separate work landing on the default branch between filing and pickup already satisfied the goal, so there is nothing left to ship.
        - *DB-only deliverable* (issue #669): the deliverable is a SQLite row written via a tusk subcommand (`tusk conventions update`, `tusk conventions add`, `tusk lint-rule add`, `tusk glossary set-definition`, etc.) — the feature branch is intentionally empty because nothing in the working tree changes.
        - *upstream-repo deliverable* (issue #999): the fix lands in an external repo declared in `tusk config`'s `project_libs` (for example `gioe/ios-libs` or `gioe/python-libs`) or another repo this host depends on — no `[TASK-N]` commits land on the host repo's default branch.

      Pass `--note "<rationale>"` in all cases and reference the converging task(s)/commit(s), the DB write performed, or the upstream PR/issue URL and commit reference (for example `Upstream PR at gioe/ios-libs#5 (dfbb4c1)`) — `tusk abandon` records it on `task_progress` as `[abandon: completed] <note>`, which is the audit signal that distinguishes this case from a normal `tusk merge` close (no `[TASK-N]` commits will be on the default branch for this task either).

    `tusk abandon` switches off the feature branch, deletes it (force), closes the session, and marks the task Done with the given `closed_reason` in one call. **Refuses** if the feature branch has commits not on the default branch — in that case use `tusk merge` to ship the work, or delete the branch manually if you really want to discard it. The optional `--note` records the decision rationale on `task_progress` so the audit trail survives. After `tusk abandon` exits 0, run `/retro` exactly as you would after `tusk merge`.

    Only after `tusk merge` (or `tusk abandon`) exits 0, return to the stable checkout captured before task-worktree handoff, then close out the /tusk skill-run so its cost is captured before `/retro` starts its own run. Do not run these commands after a failed merge or abandon attempt, and do not launch them from the task worktree after cleanup has begun:
    ```bash
    cd "$TUSK_PRIMARY_CWD"
    "$TUSK_PRIMARY_BIN" skill-run finish <run_id>
    ```

    Then emit the canonical end-of-run summary before handing off to /retro:
    ```bash
    "$TUSK_PRIMARY_BIN" task-summary <id> --format markdown
    ```

    This prints a single markdown block with the task identity, closed reason, total cost, wall/active duration, diff stats (files changed, lines added/removed, commit count), criteria counts, review pass count, and reopen count. Show it verbatim to the user — do not re-render or summarize it. Runs on both the merge and abandon paths; diff stats are filtered to commits that reference `[TASK-<id>]` so shared-branch pollution never appears in the numbers.

    Then run `/retro <id>` immediately from the same stable checkout — do not ask "shall I run retro?". Pass the task id explicitly so `/retro` attributes cost to the task you just finalized rather than picking up whichever sibling worktree closed last (issue #805). Invoke it to review the session, surface process improvements, and create follow-up tasks.

### Other Subcommands

If the user invoked a subcommand (e.g., `/tusk done`, `/tusk list`, `/tusk blocked`), read the reference file:

```
Read file: <base_directory>/SUBCOMMANDS.md
```

Skip this section when running the default workflow (no subcommand argument).
