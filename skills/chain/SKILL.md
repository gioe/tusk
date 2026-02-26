---
name: chain
description: Execute a dependency chain in parallel waves using background agents
allowed-tools: Bash, Task, Read, Glob, Grep
---

# Chain

Orchestrates parallel execution of a dependency sub-DAG. Validates the head task(s), displays the scope tree, executes the head task(s) first, then spawns parallel background agents wave-by-wave for each frontier of ready tasks until the entire chain is complete.

> **Prefer `/create-task` for all task creation.** It handles decomposition, deduplication, acceptance criteria generation, and dependency proposals in one workflow. Use `bin/tusk task-insert` directly only when scripting bulk inserts or in automated contexts where the interactive review step is not applicable.

## Arguments

Accepts one or more head task IDs and optional flags: `/chain <head_task_id1> [<head_task_id2> ...] [--on-failure skip|abort]`

When multiple IDs are provided, all heads are treated as wave 0 (run in parallel), and subsequent waves use the union of their downstream sub-DAGs.

## Flags

| Flag | Values | Description |
|------|--------|-------------|
| `--on-failure` | `skip`, `abort` | Unattended failure strategy applied when an agent finishes without completing its task. **skip** — log a warning and continue to the next wave. **abort** — stop the chain immediately and report all incomplete tasks. Omit for interactive mode (default). |

## Argument Parsing

Before Step 1, extract flags from the skill arguments:

- Parse `--on-failure <strategy>` from the argument string. Valid values: `skip`, `abort`.
- If `--on-failure` is present with a valid value, store it as `on_failure_strategy`.
- If `--on-failure` is absent or the value is invalid, `on_failure_strategy` is unset (interactive mode).
- The remaining tokens (non-flag values) are the head task IDs.

## Step 1: Validate the Head Task(s)

For each provided task ID, run:

```bash
tusk -header -column "SELECT id, summary, status, priority, complexity, assignee FROM tasks WHERE id = <task_id>"
```

- If no rows returned: abort — "Task `<task_id>` not found."
- If status is not `To Do` and not `In Progress`: abort — "Task `<task_id>` has status `<status>` — only To Do or In Progress tasks can start a chain."

## Step 2: Compute and Display Scope

```bash
tusk chain scope <head_task_id1> [<head_task_id2> ...]
```

Parse the returned JSON. The `head_task_ids` array lists all head IDs. Fetch assignees for all scope task IDs:

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
```

**Early exits:**
- If `total_tasks` equals the number of head tasks (heads only, no shared dependents): inform the user there is no chain downstream — suggest `/tusk <id>` for each head instead. Stop here.
- If all tasks are already Done: inform the user the chain is already complete. Stop here.
- If all head tasks are already Done but dependents remain: skip Step 3 and go directly to Step 4 (wave loop).

## Step 3: Execute the Head Task(s)

The head task(s) must complete before any dependents can be spawned.

**Single head:** Spawn a single background agent and monitor it as before.

**Multiple heads:** Spawn all heads as **parallel wave 0** background agents — issue all Task tool calls in a single message, just like a wave in Step 4. Monitor all of them before proceeding to Step 4.

For each head task, fetch its full details:

```bash
tusk -header -column "SELECT id, summary, description, domain, assignee, complexity FROM tasks WHERE id IN (<head_ids>)"
```

Spawn **parallel background agents** (one per head task):

```
Task tool call (for EACH head task):
  description: "TASK-<id> <first 3 words of summary>"
  subagent_type: general-purpose
  run_in_background: true
  isolation: worktree   ← include when this wave has more than one task; omit for single-task waves
  prompt: <AGENT-PROMPT.md content with {placeholders} filled from task details>
```

After spawning, store the **agent task ID** and **output file path** returned by the Task tool (this is separate from the tusk task ID). Keep a running list of all output file paths across the entire chain — these are needed for the post-chain retro in Step 6. Monitor until all head tasks reach Done status or all agents have finished:

**Monitoring loop:**

1. Wait 30 seconds:
   ```bash
   sleep 30
   ```

2. Check the task's DB status:
   ```bash
   tusk "SELECT id, status FROM tasks WHERE id IN (<head_ids>) AND status <> 'Done'"
   ```
   If the query returns no rows, all head tasks completed successfully — exit the loop and proceed to Step 4.

3. Check whether each agent has finished using `TaskOutput` with `block: false` and the agent task ID:
   - If **any agent is still running** (task not yet complete), go back to step 1.
   - If **all agents have completed** but some task statuses are NOT `Done`, those agents likely exhausted turn limits or hit unrecoverable errors. **Break out of the loop** and proceed to recovery below.

**Recovery (agents completed, tasks not Done):**

Read the agents' output files to capture any final messages.

**If `on_failure_strategy` is set**, apply it automatically without prompting:
- **skip**: Log a warning for each stuck task — "Warning: Task `<id>` (`<summary>`) did not complete (status: `<status>`). Skipping due to `--on-failure skip`." — then proceed to Step 4.
- **abort**: Stop immediately. Report that the chain was aborted due to `--on-failure abort` and list which tasks completed vs. which did not.

**Otherwise (interactive)**, report to the user:

> Agent(s) for Task(s) `<ids>` have finished, but the task status is still `<status>`.
> Agent output file(s): `<output_file_paths>`
>
> How would you like to proceed?
> 1. **Resume** — spawn new agents to continue where the previous ones left off
> 2. **Skip** — leave these tasks as-is and stop the chain
> 3. **Abort** — stop the entire chain

- **Resume**: spawn new background agents using the same Agent Prompt Template (the new agents will pick up prior progress via `tusk task-start`) and restart the monitoring loop.
- **Skip**: do not proceed to Step 4. Report that the chain was stopped.
- **Abort**: stop entirely. Report that the chain was aborted.

## Step 4: Wave Loop

Repeat the following until the chain is complete:

### 4a. Get the Frontier

```bash
tusk chain frontier <head_task_id1> [<head_task_id2> ...]
```

Parse the returned JSON. The `frontier` array contains tasks that are `To Do` with all dependencies met within the union scope.

### 4b. Check Termination

If `frontier` is empty:

```bash
tusk chain status <head_task_id1> [<head_task_id2> ...]
```

- If all scope tasks are Done: **break** — chain is complete, go to Step 5.
- If tasks remain but no frontier exists: the chain is **stuck**. Display the status output showing which tasks are blocked, and ask the user how to proceed.

### 4c. Spawn Parallel Agents

For each frontier task, fetch its full details:

```bash
tusk -header -column "SELECT id, summary, description, domain, assignee, complexity FROM tasks WHERE id IN (<frontier IDs>)"
```

Spawn **parallel background agents** — one per frontier task. Issue all Task tool calls in a single message:

```
Task tool call (for EACH frontier task):
  description: "TASK-<id> <first 3 words of summary>"
  subagent_type: general-purpose
  run_in_background: true
  isolation: worktree   ← include when the wave has more than one task; omit for single-task waves
  prompt: <AGENT-PROMPT.md content with {placeholders} filled from task details>
```

### 4d. Monitor Wave Completion

Build a map of **tusk task ID → agent task ID → output file path** for every agent spawned in this wave. Add each output file path to your running list for the post-chain retro (Step 6). Monitor until all wave tasks reach Done or all agents have finished:

**Monitoring loop:**

1. Wait 30 seconds:
   ```bash
   sleep 30
   ```

2. Check which wave tasks are still not Done:
   ```bash
   tusk "SELECT id, summary, status FROM tasks WHERE id IN (<id1>, <id2>, ...) AND status <> 'Done'"
   ```
   If the query returns no rows, all wave tasks are Done — go back to **4a**.

3. For each not-Done task, check whether its agent has finished using `TaskOutput` with `block: false` and the agent task ID:
   - If **any agent is still running**, go back to step 1. (Other agents may still be making progress that unblocks work.)
   - If **all agents have completed** but some tasks are still not Done, those agents exhausted their turn limits or hit errors. **Break out of the loop** and proceed to recovery below.

**Recovery (all agents completed, some tasks not Done):**

For each stuck task, read the agent's output file to capture any final messages.

**If `on_failure_strategy` is set**, apply it automatically without prompting:
- **skip**: Log a warning for each stuck task — "Warning: Task `<id>` (`<summary>`) did not complete (status: `<status>`). Skipping due to `--on-failure skip`." — then proceed to **4a** for the next frontier. Note: downstream tasks that depend on skipped tasks will never become ready — if the chain gets stuck later, report this to the user.
- **abort**: Stop immediately. Report that the chain was aborted due to `--on-failure abort` and list which tasks completed vs. which did not.

**Otherwise (interactive)**, report to the user:

> The following tasks' agents have finished without completing:
> - Task `<id>`: `<summary>` (status: `<status>`, agent output: `<output_file_path>`)
> - ...
>
> How would you like to proceed?
> 1. **Resume** — spawn new agents for the stuck tasks and continue the wave
> 2. **Skip** — mark these tasks as skipped and continue to the next wave
> 3. **Abort** — stop the entire chain

- **Resume**: spawn new background agents for each stuck task using the same Agent Prompt Template (agents will pick up prior progress via `tusk task-start`) and restart the monitoring loop for this wave.
- **Skip**: log a warning for each skipped task and proceed to **4a** for the next frontier. Note: downstream tasks that depend on skipped tasks will never become ready — if the chain gets stuck later, report this to the user.
- **Abort**: stop entirely. Report that the chain was aborted and list which tasks completed vs. which did not.

## Step 5: VERSION & CHANGELOG Consolidation

After all waves are complete, do a single VERSION bump and CHANGELOG update covering the entire chain. This avoids merge conflicts that occur when parallel agents each try to bump independently.

**Skip this step if:**
- No tasks in the chain touched deliverable files (skills, CLI, scripts, schema, config, install) — i.e., all tasks were docs-only or database-only changes.
- No tasks in the chain completed successfully.

**Consolidation procedure:**

1. Collect the list of completed tasks in the chain:
   ```bash
   tusk chain scope <head_task_id1> [<head_task_id2> ...]
   ```
   Filter to tasks with status = Done that were completed during this chain run.

2. Read the current VERSION and CHANGELOG:
   ```bash
   cat VERSION
   cat CHANGELOG.md | head -20
   ```

3. Increment VERSION by 1 and add a CHANGELOG entry under a new version heading. The entry should list all completed chain tasks:
   ```markdown
   ## [<new_version>] - <YYYY-MM-DD>

   ### Added/Changed/Fixed
   - [TASK-<id>] <summary>
   - [TASK-<id>] <summary>
   ...
   ```

4. Commit, push, and merge:
   ```bash
   git checkout main && git pull origin main
   git checkout -b chore/chain-<head_task_ids>-version-bump
   # Write VERSION and CHANGELOG.md
   git add VERSION CHANGELOG.md
   git commit -m "Bump VERSION to <new_version> for chain <head_task_ids>"
   git push -u origin chore/chain-<head_task_ids>-version-bump
   gh pr create --base main --title "Bump VERSION to <new_version> (chain <head_task_ids>)" --body "Consolidates VERSION bump for all tasks completed in chain <head_task_ids>."
   gh pr merge --squash --delete-branch
   ```

5. Mark deferred-to-chain criteria as done for all completed chain tasks. Individual chain agents skip VERSION/CHANGELOG criteria using `tusk criteria skip <id> --reason chain`; the orchestrator completes them here:
   ```bash
   tusk "SELECT ac.id, ac.task_id, ac.criterion FROM acceptance_criteria ac WHERE ac.is_deferred = 1 AND ac.deferred_reason = 'chain' AND ac.is_completed = 0"
   ```
   For each criterion returned, mark it done:
   ```bash
   tusk criteria done <criterion_id>
   ```

## Step 6: Post-Chain Retro Aggregation

After the chain completes, run a retrospective across all agent transcripts to capture cross-agent learnings. This uses the output file paths you collected during Steps 3 and 4.

Read the companion file for the full procedure:

```
Read file: <base_directory>/POST-CHAIN-RETRO.md
```

Where `<base_directory>` is the skill base directory shown at the top of this file.

**Skip this step if:**
- The chain was aborted before any tasks completed.
- Only a single task was in the chain (use `/retro` instead for single-task sessions).

## Step 7: Final Report

Display the completed chain status:

```bash
tusk chain status <head_task_id1> [<head_task_id2> ...]
```

Summarize:
- Total tasks completed in the chain
- Any tasks that did not complete (and current status)
- Chain execution is finished

## Agent Prompt Template

Read the template from the companion file and fill in `{placeholders}` with values from the task query:

```
Read file: <base_directory>/AGENT-PROMPT.md
```

## Error Handling

- **Agent crash / timeout**: If an agent's output shows an error or the agent returned without completing the task, report the task ID and error to the user.
- **Merge conflicts**: Multiple agents working in parallel may encounter merge conflicts. If an agent reports a conflict, flag it to the user for manual resolution or re-run the affected task.
- **Stuck chain**: If the frontier is empty but tasks remain undone, check for missing dependency links or tasks stuck In Progress. Report findings to the user.
