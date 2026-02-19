---
name: run-chain
description: Execute a dependency chain in parallel waves using background agents
allowed-tools: Bash, Task, Read, Glob, Grep
---

# Run Chain

Orchestrates parallel execution of a dependency sub-DAG. Validates the head task, displays the scope tree, executes the head task first, then spawns parallel background agents wave-by-wave for each frontier of ready tasks until the entire chain is complete.

## Arguments

Requires a head task ID: `/run-chain <head_task_id>`

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

After spawning, **monitor until the head task reaches Done status**. Poll every 30 seconds:

```bash
sleep 30
```

Then check:

```bash
tusk "SELECT status FROM tasks WHERE id = <head_task_id>"
```

Repeat until status = `Done`.

If the agent returns (TaskOutput shows completion) but the task is NOT Done, report the issue to the user and ask how to proceed.

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

Build a comma-separated list of spawned task IDs and poll until all reach Done:

```bash
sleep 30
```

```bash
tusk "SELECT id, summary, status FROM tasks WHERE id IN (<id1>, <id2>, ...) AND status <> 'Done'"
```

Repeat until the query returns no rows (all wave tasks Done), then go back to **4a**.

If all agents have returned but some tasks are not Done, report the stuck tasks and ask the user how to proceed.

## Step 5: Final Report

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
   This returns JSON with task details, prior progress, and a session_id. Hold onto the session_id.

2. **Get acceptance criteria:**
   ```
   tusk criteria list {id}
   ```
   Work through criteria in order. Mark each done as you complete it.

3. **Create a git branch** from the default branch:
   ```
   git remote set-head origin --auto 2>/dev/null
   DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
   git checkout "$DEFAULT_BRANCH" && git pull origin "$DEFAULT_BRANCH"
   git checkout -b feature/TASK-{id}-<brief-slug>
   ```
   If the branch already exists (from prior progress), just check it out.

4. **Explore the codebase** — understand what files need to change and what patterns to follow before writing any code.

5. **Implement the changes:**
   - Work through acceptance criteria as your checklist
   - After completing each criterion: `tusk criteria done <criterion_id>`
   - After each commit, log progress:
     ```
     tusk progress {id} --next-steps "<what remains to be done>"
     ```
   - If the commit includes a schema migration in bin/tusk, run `tusk migrate`

6. **Run convention lint** (advisory — fix clear violations in files you touched):
   ```
   tusk lint
   ```

7. **Push and create a PR:**
   ```
   git push -u origin <branch>
   gh pr create --base "$DEFAULT_BRANCH" --title "[TASK-{id}] <summary>" --body "## Summary\n<bullets>\n\n## Test plan\n<checklist>"
   ```

8. **Update task with PR URL:**
   ```
   tusk "UPDATE tasks SET github_pr = $(tusk sql-quote '<pr_url>'), updated_at = datetime('now') WHERE id = {id}"
   ```

9. **Self-review the PR** — read the diff, fix any issues, push follow-up commits.

10. **Merge:**
    ```
    tusk session-close <session_id>
    gh pr merge <pr_number> --squash --delete-branch
    tusk "UPDATE tasks SET status = 'Done', closed_reason = 'completed', updated_at = datetime('now') WHERE id = {id}"
    ```

IMPORTANT: Only work on Task {id}. Complete it fully — implement, commit, push, PR, merge, and mark Done. Do not expand scope beyond what the task description asks for.
```

## Error Handling

- **Agent crash / timeout**: If an agent's output shows an error or the agent returned without completing the task, report the task ID and error to the user.
- **Merge conflicts**: Multiple agents working in parallel may encounter merge conflicts. If an agent reports a conflict, flag it to the user for manual resolution or re-run the affected task.
- **Stuck chain**: If the frontier is empty but tasks remain undone, check for missing dependency links or tasks stuck In Progress. Report findings to the user.
