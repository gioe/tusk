---
name: tusk
description: Get the most important task that is ready to be worked on
allowed-tools: Bash, Task, Read, Edit, Write, Grep, Glob
---

# Tusk Skill

The primary interface for working with tasks from the project task database (via `tusk` CLI). Use this to get the next task, start working on it, and manage the full development workflow.

> Use `/create-task` for task creation — handles decomposition, deduplication, criteria, and deps. Use `tusk task-insert` only for bulk/automated inserts.

## Conditional recovery references

The core workflow keeps the normal sequence and completion gates. Read only the
linked section in `RECOVERY.md` when its stated condition occurs; do not load
the whole companion during routine startup. Return to the triggering step after
handling that condition, unless the section explicitly routes elsewhere or stops.

Resolve companion links beside this workflow file. If an existing generated
`.agents/skills` mirror lacks the adjacent companion, use the same anchor in a
recovery file under the stable primary checkout: `.codex/prompts/tusk-recovery.md`,
or the `.claude` directory followed by `skills/tusk/RECOVERY.md`; the source repo
also provides `skills/tusk/RECOVERY.md`. Resolve these paths before leaving the
stable checkout without reading their contents, and retain them across worktree
cleanup. If no companion exists when recovery is needed, report
the missing reference and stop that recovery branch rather than guessing.

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

   **Deliverable check:** If `deliverable_check_needed` from Step 1 is `true`, run:
   ```bash
   tusk check-deliverables <id>
   ```
   Before acting on the returned recommendation, read [Deliverable-check recommendations](RECOVERY.md#deliverable-check).

3. **Determine the best exploration and implementation subagent(s)** based on:
   - Task domain
   - Task assignee field (often indicates the right agent type)
   - Task description and requirements

   Exploration is always delegated in Step 5. The implementation candidate is
   used only when Step 6 routes implementation to a subagent.

4. **Confirm failure using relevant evidence** — Before exploring code for a task that fixes an existing failure, confirm the reported failure using the evidence type that actually reproduces it. Tests are authoritative only when they exercise the reported behavior; the mere presence of a focused test does not make it the reproducer.

   **When to run this step:**
   - `task_type: bug` → always confirm the failure. For a visual or screenshot bug, use a current screenshot or manual visual check against the active build/checkout. For a logic-test-backed bug, run the relevant failing test.
   - `task_type: test` AND the summary/description indicates fixing a failing or flaky test → run
   - `task_type: test` AND the summary/description indicates *writing new tests* (no existing failure to reproduce, e.g. "Add tests for X", "Write test suite for Y") → **skip this step entirely and proceed to Explore**
   - All other task types (feature, chore, docs, etc.) → skip

   1. Identify the evidence claimed to reproduce the failure in the task description and acceptance criteria: a test command, current screenshot, or manual observation.
   2. **Visual or screenshot evidence:** inspect the current screenshot or perform the stated manual visual check against the active build/checkout. If the defect is visible, treat that as the confirmed reproduction and use it as the primary diagnostic anchor in step 5 (Explore). A passing logic test that does not assert rendering must not cancel the run. Only a test that directly asserts the reported rendering defect, such as a screenshot, golden, pixel, or rendering assertion, can invalidate that visual evidence.
   3. **Logic-test evidence:** if specific tests are named, run them directly. A named test or suite is a baseline, not a reproducer, unless its assertions directly exercise the reported failure. Otherwise, use `tusk test-detect` to find the project's test command, then run the most relevant subset. If a named baseline passes but the bug's acceptance criteria require new regression coverage, add and run a focused regression test before deciding whether to cancel. Its expected pre-fix red result confirms the failure; capture it and continue to Explore. Do not cancel based only on a passing baseline.
   **Verify test execution before interpreting the exit code.** Treat zero tests, all-skipped results, or no matching tests as inconclusive even when the runner exits zero. Do not cancel the skill-run or mark the task complete on this evidence. Confirm that at least one relevant test actually executed and that its assertions exercised the reported behavior; inspect the selected files and test-name filter if execution was empty. If no existing test exercises the reported behavior, write and run a focused regression test before deciding whether the report is disproven. The issue author does not need to supply a failing test. Capture the pre-fix failure and continue to Explore; a missing test or unmatched filter is not that failure. If meaningful execution remains unavailable, record the limitation and keep the reproduction inconclusive.

   4. **If a test that directly exercises the reported failure passes**: before concluding the issue is already fixed, inspect the recorded failure evidence for time/date sensitivity. Signals include date- or year-named tests, local-date versus UTC assertions, and failure timestamps clustered in a narrow wall-clock window (use `run_started_at` / `run_ended_at` from a recorded `tusk test-precheck` verdict when available). When those signals exist, retry under a controlled timezone such as `TZ=UTC` and, when the test framework supports it, a frozen clock at the recorded failure instant. If the controlled reproduction fails, continue to Explore using that output. Only when no time-sensitive signal exists, or controlled retries still pass, does that passing evidence directly disprove the report: run `tusk skill-run cancel <run_id>`, surface that the issue may already be fixed or inaccurate, and stop before investigating further.
   5. **If a test that directly exercises the reported failure fails**: capture the failure output. Use it as the primary diagnostic anchor in step 5 (Explore).

   **State-mutating reproductions:** If the failing command writes to tracked files (e.g. `tusk version-bump`, `tusk changelog-add`, `tusk commit`, `tusk merge --rebase`), do **not** reproduce it against the active task worktree — the writes dirty the working tree and may block `tusk merge` / `tusk abandon` later. Reproduce against a throwaway location instead: `cd` into a fresh `tmp_path` repo (the integration-test pattern) and run the command there, or `git stash` the result immediately after. This is the filesystem analogue of the user-memory guidance to use a `TUSK_DB` throwaway for state-mutating DB reproductions.

5. **Explore the codebase before implementing** — always delegate this
   exploration pass to a sub-agent. Have it research:
   - What files will need to change?
   - Are there existing patterns to follow?
   - What tests already exist for this area?
   - **For each file you plan to modify**, grep it for keywords related to the feature (e.g., the concept name, the config key, the resource type). If a helper function already exists that covers what you're about to write, use it instead of duplicating the logic.

   Report findings before writing any code.

   **Stalled exploration:** After a reasonable interval without material progress, read [Stalled exploration](RECOVERY.md#stalled-exploration) before nudging, replacing, or falling back locally. An active/running status alone is not progress; do not wait indefinitely.

5b. **Declare scope before the first commit.** The commit-time scope guard reads from the authoritative `task_scope` table (TASK-471). It falls back to the `task_referenced_paths` hint cache only for legacy `scope_enforced=0` tasks; for current `scope_enforced=1` tasks, no `task_scope` rows means the commit is rejected as missing scope declaration. Before staging the first commit, run `tusk scope list <id>` to see what the table currently authorizes:

   - **If the list already covers the files you plan to touch**, proceed to commit; no action needed. Migration 73 backfilled `auto_derived` rows from your description and acceptance criteria; tasks created with `tusk task-insert --scope/--creates` have `operator_declared`/`creates` rows from the start.
   - **Reserve `operator_declared` for scope supplied during task creation or added before the task's first durable checkpoint.** `task-start` alone does not cross this provenance boundary; the boundary is the first progress checkpoint or committed criterion.
   - **If the task has no progress checkpoint and no committed criterion**, run `tusk scope add <id> <path> --reason "<why>"` before staging. The implicit source is `operator_declared` even though Step 1 has already started the task.
   - **Once a progress checkpoint or committed criterion exists**, the same implicit `tusk scope add` records `expanded_mid_task`. Keep the rationale specific so retro can distinguish healthy exploration from a decomposition miss.
   - **Once `tusk scope lock` has created the immutable scope checkpoint**, ordinary add/remove/rederive operations are refused. Use `tusk scope expand <id> <path> --reason "<why>"` for a discovered existing path, or add `--source creates` for a path this task will create. The expansion row is immediately locked and preserves its actor, time, and reason without erasing the original checkpoint. Use `tusk scope list <id> --with-status` when you need to distinguish a loose empty scope from a locked zero-row scope.
   - **If `tusk scope list` is empty on a `scope_enforced=1` task**, declare the files you plan to edit before staging. Empty scope is not a vacuous pass for current tasks; it is a metadata gap that the guard rejects before commit.
   - **If the task is a legitimately repo-wide refactor** (e.g. a rename across every skill or every Python file), it should have been created with `tusk task-insert --unbounded`. If it wasn't, `tusk scope add <id> "**" --reason "..."` is a partial workaround and uses the same checkpoint-based provenance as any other addition, but the long-term fix is to recreate the task with `--unbounded` so the guard silently passes any staged file. On tasks that already have unbounded `**` scope, redundant `tusk scope add` calls no-op with a note instead of adding dead rows.

   Externally referenced design docs (e.g. a `docs/PILLARS.md` link in the description) are background context, not scope — do not add them via `scope add` unless you actually plan to edit them.

6. **Route implementation after delegated exploration.** Wait for the
   exploration sub-agent to finish and report its findings before choosing a
   route. Then apply these rules:

   - **Local implementation is eligible only for XS/S tasks** when the
     completed exploration identifies the exact files and relevant tests, and
     the resulting change is focused and unambiguous.
   - **Delegate implementation for M/L/XL tasks**, or whenever exploration
     leaves the change broad, ambiguous, or missing exact files or tests.
   - **Explicit operator requests override the size rule.** If the operator
     asks for delegation, agents, or parallel work, delegate implementation
     even for an otherwise focused XS/S task.

   Before writing any implementation code, report the decision using one of
   these forms: `Implementation routing: local — <basis>` or
   `Implementation routing: delegated — <basis>`. On the local route, proceed
   to Step 7 in the current session. On the delegated route, assign the work to
   the chosen implementation subagent(s), then coordinate their result through
   Step 7.

   **Stalled implementation:** After a reasonable interval without material progress, read [Stalled implementation](RECOVERY.md#stalled-implementation) before nudging, replacing, or falling back locally. Local fallback for any task size requires that bounded recovery sequence; do not wait indefinitely.

7. **Implement, commit, and mark criteria done.** Work through the acceptance criteria from step 1 as your checklist — **one commit per criterion is the default**. For each criterion in order:

    **Before committing a file removal or untracking change**, read [Commit recovery](RECOVERY.md#commit-recovery) and apply its staged-deletion branch before invoking `tusk commit`.

    1. Implement the changes that satisfy it
    2. Commit and mark the criterion done atomically using `tusk commit --criteria`:
       ```bash
       tusk commit <id> "<message>" "<file1>" ["<file2>" ...] --criteria <cid>
       ```
       An alternative `-m` flag form is also supported (useful when file paths come first):
       ```bash
       tusk commit <id> "<file1>" ["<file2>" ...] -m "<message>" --criteria <cid>
       ```
       This stages the listed files, commits with the `[TASK-<id>] <message>` format and Co-Authored-By trailer, and marks the criterion done — all in one call. The criterion is bound to the new commit hash automatically. Duplicate `[TASK-N]` prefixes in the message are stripped automatically, and bare `--` separators are silently ignored.

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

    **Commit recovery:** If `tusk commit` reports a concurrent commit (exit 9), a pathspec or symlink traversal error (exit 3), unstable formatter output, a test timeout (exit 5), or failed tests (exit 2), read [Commit recovery](RECOVERY.md#commit-recovery) before retrying, bypassing a gate, using raw Git, or marking criteria. Use the matching branch and its retry limits.

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

10. **Run convention lint when needed.** Lint runs at merge time; `tusk commit` does not run lint, and `--skip-lint` is ignored by `tusk commit`. To inspect lint independently before merging:
    ```bash
    tusk lint
    ```
    Review the result: advisory warnings do not block, but blocking violations must be resolved before merge. Fix violations in task scope; do not refactor unrelated code just to satisfy lint. Use Step 12's existing exception policy for known false positives or pre-existing issues.

10b. **Prepare source-repository release metadata before final review.** Run
    this checkpoint for standalone `/tusk` work only. When `/chain` owns the
    run, skip it: the chain workflow consolidates one VERSION and CHANGELOG
    update after every head completes.

    Resolve the active checkout-local Tusk wrapper using this candidate order:
    `bin/tusk`, `tusk/bin/tusk`, then `.claude/bin/tusk`. Follow its
    complete symlink chain, and read the sibling `install-mode` marker. A
    compound marker ending in `-consumer` means this is a consumer project:
    skip the rest of this checkpoint. A marker ending in `-source`, a legacy
    plain marker without a role suffix, or a missing marker means this is the
    Tusk source repository and the checkpoint applies. Do not infer the role
    from CWD names or the presence of source-looking files.

    For a source run, compare the task branch with `origin/<default>` using
    `tusk git-default-branch`. If the committed diff changes any path guarded
    by `hooks/git/version-bump-check.sh` (`bin/*`, `skills/*`,
    `config.default.json`, or `install.sh`), VERSION and CHANGELOG.md must be
    part of task scope and committed before Step 11:

    ```bash
    tusk scope add <id> VERSION --reason "Source release metadata required before review"
    tusk scope add <id> CHANGELOG.md --reason "Source release metadata required before review"
    tusk version-bump
    tusk changelog-add <id>
    tusk commit <id> "Prepare source release metadata before review" VERSION CHANGELOG.md
    ```

    If `tusk scope list <id> --with-status` reports a checkpoint, replace each
    `scope add` above with `scope expand` and keep the same required reason.

    First check whether VERSION already differs from `origin/<default>`. If it
    does, do **not** bump VERSION again; verify that CHANGELOG.md already has
    the current version and task entry, add only the missing changelog entry if
    needed, and commit any remaining metadata before review. This keeps retries
    and resumed sessions to one release bump.

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

    The merge path runs a pre-merge lint gate by default. If `tusk merge` exits 6, blocking lint violations prevented the merge; fix them and retry. A lint timeout exits 8 and also prevents the merge: diagnose the hung check or its timeout before retrying. Advisory warnings do not block. For a known false positive or pre-existing issue, use `--skip-lint` to skip only lint. Use `--skip-verify` only when the broader bypass is required; it also skips the merge lint gate.

    `tusk merge` refuses to proceed while ordinary non-deferred criteria are still open. Complete them, or use Step 9's explicit post-merge verification deferral pattern when the check is impossible before merge.

    **Merge recovery:** For an already-merged task, divergence, a missing branch, sibling-worktree/remote/database problems, or partial cleanup (exit 3), read [Merge recovery](RECOVERY.md#merge-recovery) before selecting a fallback. Merge exit 3 means the merge and task completion succeeded with cleanup outstanding: preserve unexpected files and continue finalization from the stable checkout, reporting the cleanup warning.

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

    **Finalization outcome gate:** Continue after merge exit 0, merge exit 3 (partial cleanup), or abandon exit 0. For every other exit code, stop finalization and handle the failed command before retrying; do not emit a success summary. Return to the stable checkout captured before task-worktree handoff, then close the skill-run before retro starts its own run. Never launch these commands from a task worktree after cleanup has begun:
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
