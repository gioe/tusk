---
name: next-task
description: Get the most important task that is ready to be worked on
allowed-tools: Bash, Task, Read, Edit, Write, Grep, Glob
---

# Next Task Skill

The primary interface for working with tasks from the project task database (via `tusk` CLI). Use this to get the next task, start working on it, and manage the full development workflow.

## Setup: Discover Project Config

Before any operation that needs domain or agent values, run:

```bash
tusk config domains
tusk config agents
```

Use the returned values (not hardcoded ones) when validating or inserting tasks.

## Commands

### Get Next Task (default - no arguments)

Finds the highest-priority task that is ready to work on (no incomplete dependencies) and **automatically begins working on it**.

```bash
tusk -header -column "
SELECT t.id, t.summary, t.priority, t.priority_score, t.domain, t.assignee, t.description
FROM tasks t
WHERE t.status = 'To Do'
  AND NOT EXISTS (
    SELECT 1 FROM task_dependencies d
    JOIN tasks blocker ON d.depends_on_id = blocker.id
    WHERE d.task_id = t.id AND blocker.status != 'Done'
  )
ORDER BY t.priority_score DESC, t.id
LIMIT 1;
"
```

**Note**: The `priority_score` is pre-computed by `/groom-backlog` and factors in priority level, how many tasks this unblocks, and task age.

After finding the next ready task, **immediately proceed to the "Begin Work on a Task" workflow** using the retrieved task ID. Do not wait for user confirmation.

### Begin Work on a Task (with task ID argument)

When called with a task ID (e.g., `/next-task 6`), begin the full development workflow:

**Follow these steps IN ORDER:**

1. **Fetch the task** from the database:
   ```bash
   tusk -header -column "SELECT * FROM tasks WHERE id = <id>"
   ```

2. **Update the task status** to In Progress:
   ```bash
   tusk "UPDATE tasks SET status = 'In Progress', updated_at = datetime('now') WHERE id = <id>"
   ```

3. **Extract task details** including:
   - Summary
   - Description
   - Priority
   - Domain
   - Assignee

4. **Create a new git branch IMMEDIATELY**:
   - Format: `feature/TASK-<id>-brief-description`
   - Commands:
     ```bash
     git checkout main && git pull origin main
     git checkout -b feature/TASK-<id>-brief-description
     ```

5. **Determine the best subagent(s)** based on:
   - Task domain
   - Task assignee field (often indicates the right agent type)
   - Task description and requirements

6. **Explore the codebase before implementing** — use a sub-agent to research:
   - What files will need to change?
   - Are there existing patterns to follow?
   - What tests already exist for this area?

   Report findings before writing any code.

7. **Delegate the work** to the chosen subagent(s).

8. **Create atomic commits** as you complete logical units of work.
   - All commits should be on the feature branch, NOT main.

9. **Review the code locally** before considering the work complete.

10. **Push the branch and create a PR**:
    ```bash
    git push -u origin feature/TASK-<id>-description
    gh pr create --title "[TASK-<id>] Brief task description" --body "..."
    ```
    Capture the PR URL from the output.

11. **Update the task with the PR URL**:
    ```bash
    tusk "UPDATE tasks SET github_pr = '<pr_url>', updated_at = datetime('now') WHERE id = <id>"
    ```

12. **Review loop — iterate until approved**:

    ```
    ┌─► Poll for review
    │         │
    │         ▼
    │   Analyze review
    │         │
    │         ▼
    │   ┌─────────────┐
    │   │ Approved?   │───Yes──► Exit loop
    │   └─────────────┘
    │         │ No
    │         ▼
    │   Address comments
    │         │
    │         ▼
    │   Push fixes
    │         │
    └─────────┘
    ```

    **Category A — Address Immediately (must fix in this PR):**
    - Security concerns, bugs, breaking changes
    - Missing tests for code introduced/modified in this PR
    - Performance issues, type errors, missing error handling

    The bar is: if the reviewer comments on code this PR touches, fix it now.

    For each Category A comment:
    1. Read the relevant file(s)
    2. Make the code fix
    3. Commit: `[TASK-<id>] Address PR review: <brief description>`

    **Category B — Defer to backlog (cosmetic only):**
    - Pure style preferences not affecting correctness
    - Suggestions about pre-existing code NOT touched by this PR
    - Aspirational ideas about unrelated modules

    For each Category B comment:
    1. **Check for duplicates first** using `/check-dupes`:
       ```bash
       python3 scripts/check_duplicates.py check "[Deferred] <brief description>" --domain <domain>
       ```
    2. Create a deferred task (with 60-day expiry):
       ```bash
       tusk "INSERT INTO tasks (summary, description, status, priority, domain, created_at, updated_at, expires_at)
         VALUES ('[Deferred] <brief description>', 'Deferred from PR #<pr_number> review for TASK-<id>.

Original comment: <comment text>

Reason deferred: <why this can wait>', 'To Do', 'Low', '<domain>', datetime('now'), datetime('now'), datetime('now', '+60 days'))"
       ```

13. **PR approved — finalize and merge**:

    ```bash
    gh pr merge $PR_NUMBER --squash --delete-branch
    ```

    Update task status:
    ```bash
    tusk "UPDATE tasks SET status = 'Done', closed_reason = 'completed', updated_at = datetime('now') WHERE id = <id>"
    ```

14. **Check for newly unblocked tasks**:
    ```bash
    tusk -header -column "
    SELECT t.id, t.summary, t.priority
    FROM tasks t
    JOIN task_dependencies d ON t.id = d.task_id
    WHERE d.depends_on_id = <id> AND t.status = 'To Do'
    "
    ```

### Mark Task as Done

When called with `done <id>`:

```bash
tusk "UPDATE tasks SET status = 'Done', closed_reason = 'completed', updated_at = datetime('now') WHERE id = <id>"
```

Then show newly unblocked tasks.

### View Task Details

When called with `view <id>`:

```bash
tusk -header -column "SELECT * FROM tasks WHERE id = <id>"
```

### List Top N Ready Tasks

When called with `list <n>` or just a number:

```bash
tusk -header -column "
SELECT t.id, t.summary, t.priority, t.domain, t.assignee
FROM tasks t
WHERE t.status = 'To Do'
  AND NOT EXISTS (
    SELECT 1 FROM task_dependencies d
    JOIN tasks blocker ON d.depends_on_id = blocker.id
    WHERE d.task_id = t.id AND blocker.status != 'Done'
  )
ORDER BY t.priority_score DESC, t.id
LIMIT <n>;
"
```

### Filter by Domain

When called with `domain <value>`: Get next ready task for that domain only.

### Filter by Assignee

When called with `assignee <value>`: Get next ready task for that assignee only.

### Show Blocked Tasks

When called with `blocked`:

```bash
tusk -header -column "
SELECT t.id, t.summary, t.priority,
  (SELECT GROUP_CONCAT(d.depends_on_id) FROM task_dependencies d WHERE d.task_id = t.id) as blocked_by
FROM tasks t
WHERE t.status = 'To Do'
  AND EXISTS (
    SELECT 1 FROM task_dependencies d
    JOIN tasks blocker ON d.depends_on_id = blocker.id
    WHERE d.task_id = t.id AND blocker.status != 'Done'
  )
ORDER BY t.id
"
```

### Show In Progress Tasks

When called with `wip` or `in-progress`:

```bash
tusk -header -column "SELECT id, summary, priority, domain, assignee, github_pr FROM tasks WHERE status = 'In Progress'"
```

### Preview Next Task (without starting)

When called with `preview`: Show the next ready task but do NOT start working on it.

```bash
tusk -header -column "
SELECT t.id, t.summary, t.priority, t.domain, t.assignee, t.description
FROM tasks t
WHERE t.status = 'To Do'
  AND NOT EXISTS (
    SELECT 1 FROM task_dependencies d
    JOIN tasks blocker ON d.depends_on_id = blocker.id
    WHERE d.task_id = t.id AND blocker.status != 'Done'
  )
ORDER BY t.priority_score DESC, t.id
LIMIT 1;
"
```

## Argument Parsing Summary

| Argument | Action |
|----------|--------|
| (none) | Get next ready task and automatically start working on it |
| `<id>` | Begin full workflow on task #id |
| `list <n>` | Show top N ready tasks |
| `done <id>` | Mark task as Done |
| `view <id>` | Show full task details |
| `domain <value>` | Filter next task by domain |
| `assignee <value>` | Filter next task by assignee |
| `blocked` | Show all blocked tasks |
| `wip` | Show all In Progress tasks |
| `preview` | Show next ready task without starting it |

## Canonical Values

Run `tusk config` to see all valid values for this project. Using non-canonical values will be rejected by SQLite triggers.

### Closed Reason (set when status = Done)
`completed`, `expired`, `wont_do`, `duplicate`

Always set `closed_reason` when marking a task Done.

## Important Guidelines

- Write tests for all tasks unless the task is untestable
- Ask clarifying questions if task requirements are ambiguous
- Make sure work is delegated to the correct subagent based on the assignee field
- Mark complete only when fully implemented and tested
