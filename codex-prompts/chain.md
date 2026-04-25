# Chain — Sequential Dependency Sub-DAG Execution (Codex)

Orchestrates execution of a dependency sub-DAG. Validates the head task(s),
displays the scope tree, executes the head task(s) first, then walks each
frontier of ready tasks until the entire chain is complete.

> **Conventions:** Run `tusk conventions search <topic>` for project rules.
> Do not restate convention text inline — it drifts from the DB.

> **Sequential execution — no parallel sub-agents.** Codex has no Task
> tool for spawning background agents. Every task in the chain runs
> **sequentially in the current Codex session**, one at a time. Do not
> attempt to launch parallel Codex sessions, background processes, or
> worktree splits from within this prompt. The tradeoff: chains take
> longer end-to-end than the Claude Code parallel variant, but they are
> deterministic and never produce merge conflicts between siblings.

> Use `create-task.md` for task creation — handles decomposition,
> deduplication, criteria, and deps. Use `tusk task-insert` only for
> bulk/automated inserts.

## Arguments

Accepts one or more head task IDs and optional flags:
`/chain <head_task_id1> [<head_task_id2> ...] [--on-failure skip|abort]`

When multiple IDs are provided, all heads are processed first (in
sequence), and subsequent waves use the union of their downstream
sub-DAGs.

## Flags

| Flag | Values | Description |
|------|--------|-------------|
| `--on-failure` | `skip`, `abort` | Unattended failure strategy applied when a task does not reach Done. **skip** — log a warning and continue to the next task. **abort** — stop the chain immediately and report all incomplete tasks. Omit for interactive mode (default). |

## Argument Parsing

Before Step 1, extract flags from the prompt arguments:

- Parse `--on-failure <strategy>` from the argument string. Valid values:
  `skip`, `abort`.
- If `--on-failure` is present with a valid value, store it as
  `on_failure_strategy`.
- If `--on-failure` is absent or the value is invalid,
  `on_failure_strategy` is unset (interactive mode).
- The remaining tokens (non-flag values) are the head task IDs.

## Step 0: Start Cost Tracking

Record the start of this chain run so cost can be captured when the chain
finishes:

```bash
tusk skill-run start chain --task-id <head_task_id>
```

**Pass `--task-id` only when exactly one head task ID was provided.** With
multiple heads the chain spans more than one task and cost can't be
attributed to a single row — omit `--task-id` in that case:

```bash
tusk skill-run start chain
```

This prints `{"run_id": N, "started_at": "...", "task_id": N | null}`.
Capture `run_id` — it's referenced by every exit path below.

> **Early-exit cleanup:** If any step below causes the chain to stop
> before reaching the final report in Step 7, first call
> `tusk skill-run cancel <run_id>` to close the open row, then stop.
> Otherwise the row lingers as `(open)` in `tusk skill-run list` forever.

## Step 1: Validate the Head Task(s)

For each provided task ID, run:

```bash
tusk -header -column "SELECT id, summary, status, priority, complexity, assignee FROM tasks WHERE id = <task_id>"
```

- If no rows returned: run `tusk skill-run cancel <run_id>`, then abort —
  "Task `<task_id>` not found."
- If status is not `To Do` and not `In Progress`: run
  `tusk skill-run cancel <run_id>`, then abort — "Task `<task_id>` has
  status `<status>` — only To Do or In Progress tasks can start a chain."

## Step 2: Compute and Display Scope

```bash
tusk chain scope <head_task_id1> [<head_task_id2> ...]
```

Parse the returned JSON. The `head_task_ids` array lists all head IDs.
Fetch assignees for all scope task IDs:

```bash
tusk -header -column "SELECT id, assignee FROM tasks WHERE id IN (<comma-separated scope IDs>)"
```

Display the sub-DAG as an indented tree grouped by depth:

```
Chain scope for Task(s) <id(s)>: <summary(ies)>
══════════════════════════════════════════════════════════════

Depth 0 (head):
  [<id>] <summary>  (<status> | <complexity> | <assignee or "unassigned">)

Depth 1:
  [<id>] <summary>  (<status> | <complexity> | <assignee or "unassigned">)
  [<id>] <summary>  (<status> | <complexity> | <assignee or "unassigned">)

Depth 2:
  [<id>] <summary>  (<status> | <complexity> | <assignee or "unassigned">)

Progress: <completed>/<total> tasks completed (<percent>%)
Execution: SEQUENTIAL — one task at a time in this session
```

**Scope validation:**

```bash
tusk chain validate-scope <head_task_id1> [<head_task_id2> ...]
```

Parse the returned JSON. It has two fields: `scope_type` and
`skip_head_execution`:

- **`no-downstream`**: run `tusk skill-run cancel <run_id>`, inform the
  user there is no chain downstream — suggest `tusk.md` for `<id>` for
  each head instead. Stop here.
- **`all-done`**: run `tusk skill-run cancel <run_id>`, inform the user
  the chain is already complete. Stop here.
- **`heads-done-only`** (`skip_head_execution: true`): all head tasks are
  already Done — skip Step 3 and go directly to Step 4 (wave loop).
- **`active-chain`**: proceed normally to Step 3.

## Step 3: Execute the Head Task(s)

The head task(s) must complete before any dependents can be processed.

**Sequential execution.** Process each head one at a time in the order
returned by `tusk chain scope`. For each head:

1. Fetch its full details:
   ```bash
   tusk task-get <head_id>
   ```
2. Follow `tusk.md` Step 1 onward for `<head_id>` — start the task,
   create a branch, explore, implement, commit each criterion, run lint,
   run review-commits if configured, merge.
3. **Skip VERSION/CHANGELOG per-head** — that lands in Step 5 as a single
   consolidation commit. If the head's acceptance criteria require a
   VERSION bump or CHANGELOG entry, defer those criteria with:
   ```bash
   tusk criteria skip <criterion_id> --reason chain
   ```
   Deferred criteria do not block `tusk task-done`; the chain orchestrator
   will mark them done after the consolidation step in Step 5.
4. After the head's `tusk merge` (or `tusk abandon`) exits 0, capture its
   resulting status (`Done` or otherwise) before continuing.

**Failure handling between heads.** If a head ends without reaching
`Done` status:

- **`on_failure_strategy = skip`**: log a warning ("Warning: Task `<id>`
  did not complete — status `<status>`. Skipping due to `--on-failure
  skip`.") and continue to the next head.
- **`on_failure_strategy = abort`**: run `tusk skill-run cancel <run_id>`,
  then stop immediately. Report which heads completed vs. did not.
- **Interactive (no flag)**: ask the user:
  > Task `<id>` did not complete (status: `<status>`). How would you like
  > to proceed?
  > 1. **Resume** — re-run `tusk.md` for `<id>`
  > 2. **Skip** — leave as-is and continue to the next head
  > 3. **Abort** — stop the entire chain
  Apply the user's choice and continue.

## Step 4: Wave Loop

Repeat the following until the chain is complete:

### 4a. Get Frontier and Check Termination

```bash
tusk chain frontier-check <head_task_id1> [<head_task_id2> ...]
```

Parse the returned JSON. It has two fields:
- `status` — one of `complete`, `stuck`, or `continue`
- `frontier` — array of ready tasks (non-empty only when
  `status=continue`)

### 4b. Branch on Status

- **`complete`**: all tasks in the subgraph are Done — **break** out of
  the wave loop and go to Step 5.
- **`stuck`**: tasks remain but no ready tasks exist in the frontier.
  Display the chain status for context:
  ```bash
  tusk chain status <head_task_id1> [<head_task_id2> ...] --format text
  ```
  Show the output to the user and ask how to proceed. If the user chooses
  to stop the chain here, run `tusk skill-run cancel <run_id>` before
  returning.
- **`continue`**: the `frontier` array contains at least one ready task
  — proceed to Step 4c.

### 4c. Process the Frontier Sequentially

For each task in the `frontier` array, in the order returned:

1. Follow `tusk.md` Step 1 onward for that task ID — start, branch,
   explore, implement, commit, lint, review-commits if configured, merge.
2. **Skip VERSION/CHANGELOG per-task** — defer those criteria with
   `tusk criteria skip <criterion_id> --reason chain` if the task lists
   them as criteria. Consolidation lands in Step 5.
3. After `tusk merge` (or `tusk abandon`) exits 0, capture the task's
   resulting status.

**Failure handling within a wave.** Apply the same rules as Step 3:

- **`on_failure_strategy = skip`**: log a warning and continue to the
  next frontier task. Note that downstream tasks depending on a skipped
  task will never become ready — if the chain gets stuck later in 4b,
  surface this to the user.
- **`on_failure_strategy = abort`**: run `tusk skill-run cancel <run_id>`
  and stop immediately.
- **Interactive (no flag)**: ask resume/skip/abort and apply the user's
  choice.

After processing all tasks in the current wave, return to **4a** to
recompute the frontier.

## Step 5: VERSION & CHANGELOG Consolidation

After all waves are complete, do a single VERSION bump and CHANGELOG
update covering the entire chain.

**Skip this step if:**
- No tasks in the chain touched deliverable files (skills, CLI, scripts,
  schema, config, install) — i.e., all tasks were docs-only or
  database-only changes.
- No tasks in the chain completed successfully.

**Consolidation procedure:**

1. Collect the list of completed tasks in the chain:
   ```bash
   tusk chain scope <head_task_id1> [<head_task_id2> ...]
   ```
   Filter to tasks with `status = Done` that were completed during this
   chain run.

2. Bump VERSION and update CHANGELOG in one step each:
   ```bash
   new_version=$(tusk version-bump)
   tusk changelog-add $new_version <task_id1> [<task_id2> ...]
   ```
   `tusk version-bump` reads VERSION, increments by 1, writes it back,
   stages it, and prints the new version number. `tusk changelog-add`
   prepends a dated `## [N] - YYYY-MM-DD` heading to `CHANGELOG.md` with a
   bullet for each task ID, stages `CHANGELOG.md`, then prints the
   inserted block to stdout for review.

3. Review the changelog output, then commit, push, and merge:
   ```bash
   DEFAULT_BRANCH=$(tusk git-default-branch)
   git checkout "$DEFAULT_BRANCH" && git pull origin "$DEFAULT_BRANCH"
   git checkout -b chore/chain-<head_task_ids>-version-bump
   git commit -m "Bump VERSION to <new_version> for chain <head_task_ids>"
   git push -u origin chore/chain-<head_task_ids>-version-bump
   gh pr create --base "$DEFAULT_BRANCH" \
     --title "Bump VERSION to <new_version> (chain <head_task_ids>)" \
     --body "Consolidates VERSION bump for all tasks completed in chain <head_task_ids>."
   gh pr merge --squash --delete-branch
   ```

4. Mark deferred-to-chain criteria as done for all completed chain tasks:
   ```bash
   tusk criteria finish-deferred --reason chain <task_id1> [<task_id2> ...]
   ```
   This marks all `is_deferred=1, deferred_reason=chain, is_completed=0`
   criteria for the given tasks and prints `{"marked": N}`.

## Step 6: Post-Chain Retro

After the chain completes, run a retrospective across the chain.

**Skip this step if:**
- The chain was aborted before any tasks completed.
- Only a single task was in the chain (use `retro.md` instead for
  single-task sessions).

For multi-task chains, follow `retro.md` once with the most recently
closed chain task as the anchor — it pulls cross-retro themes via
`tusk retro <task_id>` and surfaces patterns visible across the chain's
sessions.

## Step 7: Final Report

Display the completed chain status:

```bash
tusk chain status <head_task_id1> [<head_task_id2> ...] --format text
```

The default output is compact JSON; `--format text` is used here for the
human-readable final report. Summarize:
- Total tasks completed in the chain
- Any tasks that did not complete (and current status)
- Chain execution is finished

Then close out the chain skill-run so its cost is captured:

```bash
tusk skill-run finish <run_id>
```

## Error Handling

- **Task fails to complete**: see the failure-handling rules in Steps 3
  and 4c — apply `on_failure_strategy` (skip/abort) or ask the user.
- **Merge conflicts during a wave**: in sequential mode this is rare —
  each task merges cleanly onto the latest default branch before the
  next starts. If a conflict appears, resolve it inside the failing
  task's `tusk merge --rebase` flow, then continue.
- **Stuck chain**: if the frontier becomes empty but tasks remain
  undone, check for missing dependency links or tasks stuck `In
  Progress`. Report findings to the user.
