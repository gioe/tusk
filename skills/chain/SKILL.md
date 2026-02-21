---
name: chain
description: Execute a dependency chain in parallel waves using background agents
allowed-tools: Bash, Task, Read, Glob, Grep
---

# Chain

Orchestrates parallel execution of a dependency sub-DAG. Validates the head task, displays the scope tree, executes the head task first, then spawns parallel background agents wave-by-wave for each frontier of ready tasks until the entire chain is complete.

## Arguments

Requires a head task ID: `/chain <head_task_id>`

## Step 1: Validate the Head Task

```bash
tusk -header -column "SELECT id, summary, status, priority, complexity, assignee FROM tasks WHERE id = <head_task_id>"
```

- If no rows returned: abort — "Task `<head_task_id>` not found."
- If status is not `To Do` and not `In Progress`: abort — "Task `<head_task_id>` has status `<status>` — only To Do or In Progress tasks can start a chain."

## Step 2: Compute and Display Scope

```bash
tusk chain scope <head_task_id>
```

Parse the returned JSON. Fetch assignees for all scope task IDs:

```bash
tusk -header -column "SELECT id, assignee FROM tasks WHERE id IN (<comma-separated scope IDs>)"
```

Display the sub-DAG as an indented tree grouped by depth:

```
Chain scope for Task <id>: <summary>
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
- If `total_tasks` is 1 (head only, no dependents): inform the user this is a single task with no chain — suggest `/next-task <id>` instead. Stop here.
- If all tasks are already Done: inform the user the chain is already complete. Stop here.
- If the head task is already Done but dependents remain: skip Step 3 and go directly to Step 4 (wave loop).

## Step 3: Execute the Head Task

The head task must complete before any dependents can be spawned.

Fetch the head task's full details for the agent prompt:

```bash
tusk -header -column "SELECT id, summary, description, domain, assignee, complexity FROM tasks WHERE id = <head_task_id>"
```

Spawn a **single background agent** using the Task tool:

```
Task tool call:
  description: "TASK-<id> <first 3 words of summary>"
  subagent_type: general-purpose
  run_in_background: true
  prompt: <use the Agent Prompt Template below, filled with task details>
```

After spawning, store the **agent task ID** and **output file path** returned by the Task tool (this is separate from the tusk task ID). Keep a running list of all output file paths across the entire chain — these are needed for the post-chain retro in Step 6. Monitor until the head task reaches Done status or the agent finishes without completing:

**Monitoring loop:**

1. Wait 30 seconds:
   ```bash
   sleep 30
   ```

2. Check the task's DB status:
   ```bash
   tusk "SELECT status FROM tasks WHERE id = <head_task_id>"
   ```
   If status = `Done`, the task completed successfully — exit the loop and proceed to Step 4.

3. Check whether the agent has finished using `TaskOutput` with `block: false` and the agent task ID:
   - If the agent is **still running** (task not yet complete), go back to step 1.
   - If the agent has **completed** but the task status is NOT `Done`, the agent likely exhausted its turn limit or hit an unrecoverable error. **Break out of the loop** and proceed to recovery below.

**Recovery (agent completed, task not Done):**

Read the agent's output file to capture any final messages, then report to the user:

> Agent for Task `<id>` has finished, but the task status is still `<status>`.
> Agent output file: `<output_file_path>`
>
> How would you like to proceed?
> 1. **Resume** — spawn a new agent to continue where the previous one left off
> 2. **Skip** — leave this task as-is and stop the chain
> 3. **Abort** — stop the entire chain

- **Resume**: spawn a new background agent using the same Agent Prompt Template (the new agent will pick up prior progress via `tusk task-start`) and restart the monitoring loop.
- **Skip**: do not proceed to Step 4. Report that the chain was stopped.
- **Abort**: stop entirely. Report that the chain was aborted.

## Step 4: Wave Loop

Repeat the following until the chain is complete:

### 4a. Get the Frontier

```bash
tusk chain frontier <head_task_id>
```

Parse the returned JSON. The `frontier` array contains tasks that are `To Do` with all dependencies met within scope.

### 4b. Check Termination

If `frontier` is empty:

```bash
tusk chain status <head_task_id>
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
  prompt: <use the Agent Prompt Template below, filled with that task's details>
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

For each stuck task, read the agent's output file to capture any final messages. Then report to the user:

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
   tusk chain scope <head_task_id>
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
   git checkout -b chore/chain-<head_task_id>-version-bump
   # Write VERSION and CHANGELOG.md
   git add VERSION CHANGELOG.md
   git commit -m "Bump VERSION to <new_version> for chain <head_task_id>"
   git push -u origin chore/chain-<head_task_id>-version-bump
   gh pr create --base main --title "Bump VERSION to <new_version> (chain <head_task_id>)" --body "Consolidates VERSION bump for all tasks completed in chain <head_task_id>."
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
tusk chain status <head_task_id>
```

Summarize:
- Total tasks completed in the chain
- Any tasks that did not complete (and current status)
- Chain execution is finished

## Agent Prompt Template

Use this template for every spawned agent. Replace `{placeholders}` with actual values from the task query.

```
You are an autonomous agent working on a single task as part of a dependency chain.

**Task {id}: {summary}**

Description:
{description}

Domain: {domain}
Assignee: {assignee}
Complexity: {complexity}

---

**Instructions — follow the /next-task workflow end-to-end:**

1. **Start the task:**
   ```
   tusk task-start {id}
   ```
   This returns JSON with task details, prior progress, criteria, and a session_id. Hold onto the session_id. The `criteria` array contains acceptance criteria — work through them in order and mark each done as you complete it.

2. **Create a git branch** from the default branch:
   ```
   git remote set-head origin --auto 2>/dev/null
   DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
   git checkout "$DEFAULT_BRANCH" && git pull origin "$DEFAULT_BRANCH"
   git checkout -b feature/TASK-{id}-<brief-slug>
   ```
   If the branch already exists (from prior progress), just check it out.

3. **Explore the codebase** — understand what files need to change and what patterns to follow before writing any code.

4. **Implement the changes:**
   - Work through acceptance criteria as your checklist
   - After completing each criterion: `tusk criteria done <criterion_id>`
   - After each commit, log progress:
     ```
     tusk progress {id} --next-steps "<what remains to be done>"
     ```
   - If the commit includes a schema migration in bin/tusk, run `tusk migrate`

5. **Run convention lint** (advisory — fix clear violations in files you touched):
   ```
   tusk lint
   ```

6. **Push and create a PR:**
   ```
   git push -u origin <branch>
   gh pr create --base "$DEFAULT_BRANCH" --title "[TASK-{id}] <summary>" --body "## Summary\n<bullets>\n\n## Test plan\n<checklist>"
   ```

7. **Update task with PR URL:**
   ```
   tusk "UPDATE tasks SET github_pr = $(tusk sql-quote '<pr_url>'), updated_at = datetime('now') WHERE id = {id}"
   ```

8. **Self-review the PR** — read the diff, fix any issues, push follow-up commits.

9. **Merge:**
    ```
    tusk session-close <session_id>
    gh pr merge <pr_number> --squash --delete-branch
    tusk task-done {id} --reason completed
    ```

IMPORTANT: Only work on Task {id}. Complete it fully — implement, commit, push, PR, merge, and mark Done. Do not expand scope beyond what the task description asks for.

IMPORTANT: Do NOT bump the VERSION file or update CHANGELOG.md — version bumps are handled by a single consolidation step after the entire chain completes. Skipping this avoids merge conflicts when multiple agents run in parallel.

IMPORTANT: If the task has acceptance criteria that require bumping VERSION or updating CHANGELOG, mark those criteria as deferred instead of using --force:
```
tusk criteria skip <criterion_id> --reason chain
```
Deferred criteria do not block `tusk task-done`, and the chain orchestrator will mark them done after the consolidation step.
```

## Error Handling

- **Agent crash / timeout**: If an agent's output shows an error or the agent returned without completing the task, report the task ID and error to the user.
- **Merge conflicts**: Multiple agents working in parallel may encounter merge conflicts. If an agent reports a conflict, flag it to the user for manual resolution or re-run the affected task.
- **Stuck chain**: If the frontier is empty but tasks remain undone, check for missing dependency links or tasks stuck In Progress. Report findings to the user.
