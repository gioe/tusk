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
SELECT t.id, t.summary, t.priority, t.priority_score, t.domain, t.assignee, t.complexity, t.description
FROM tasks t
WHERE t.status = 'To Do'
  AND NOT EXISTS (
    SELECT 1 FROM task_dependencies d
    JOIN tasks blocker ON d.depends_on_id = blocker.id
    WHERE d.task_id = t.id AND blocker.status <> 'Done'
  )
ORDER BY t.priority_score DESC, t.id
LIMIT 1;
"
```

**Empty backlog**: If the query returns no rows, the backlog has no ready tasks. Check why:

```bash
tusk -header -column "SELECT status, COUNT(*) as count FROM tasks GROUP BY status"
```

- If there are **no tasks at all** (or all are Done): inform the user the backlog is empty and suggest running `/create-task` to add new work.
- If there are **To Do tasks but all are blocked**: inform the user and suggest running `/next-task blocked` to see what's holding them up.
- If there are **In Progress tasks**: inform the user and suggest running `/next-task wip` to check on active work.

Do **not** suggest `/groom-backlog` or `/retro` when there are no ready tasks — those skills require an active backlog or session history to be useful.

**Note**: The `priority_score` is pre-computed by `/groom-backlog` using WSJF (Weighted Shortest Job First) scoring — it factors in priority level, how many tasks this unblocks, and divides by complexity weight (XS=1, S=2, M=3, L=5, XL=8) so small high-value tasks rank higher.

**Complexity warning**: If the selected task has complexity **L** or **XL**, display a warning to the user before proceeding:

> **Note: This is a large task (complexity: L/XL) — expect 3+ sessions to complete.**

Then ask the user whether to proceed or request a smaller task. If the user chooses a smaller task, re-run the query excluding L and XL:

```bash
tusk -header -column "
SELECT t.id, t.summary, t.priority, t.priority_score, t.domain, t.assignee, t.complexity, t.description
FROM tasks t
WHERE t.status = 'To Do'
  AND t.complexity NOT IN ('L', 'XL')
  AND NOT EXISTS (
    SELECT 1 FROM task_dependencies d
    JOIN tasks blocker ON d.depends_on_id = blocker.id
    WHERE d.task_id = t.id AND blocker.status <> 'Done'
  )
ORDER BY t.priority_score DESC, t.id
LIMIT 1;
"
```

If no smaller task is available, inform the user and offer to proceed with the original L/XL task.

After the user confirms (or if the task is not L/XL), **immediately proceed to the "Begin Work on a Task" workflow** using the retrieved task ID. Do not wait for additional user confirmation.

### Begin Work on a Task (with task ID argument)

When called with a task ID (e.g., `/next-task 6`), begin the full development workflow:

**Follow these steps IN ORDER:**

1. **Start the task** — fetch details, check progress, create/reuse session, and set status in one call:
   ```bash
   tusk task-start <id>
   ```
   This returns a JSON blob with three keys:
   - `task` — full task row (summary, description, priority, domain, assignee, etc.)
   - `progress` — array of prior progress checkpoints (most recent first). If non-empty, the first entry's `next_steps` tells you exactly where to pick up. Skip steps you've already completed (branch may already exist, some commits may already be made). Use `git log --oneline` on the existing branch to see what's already been done.
   - `session_id` — the session ID to use for the duration of the workflow (reuses an open session if one exists, otherwise creates a new one)

   Hold onto `session_id` from the JSON — it will be used to close the session when the task is done.

2. **Create a new git branch IMMEDIATELY** (skip if resuming and branch already exists):
   - Format: `feature/TASK-<id>-brief-description`
   - First, detect the repo's default branch:
     ```bash
     git remote set-head origin --auto 2>/dev/null
     DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
     if [ -z "$DEFAULT_BRANCH" ]; then
       DEFAULT_BRANCH=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo "main")
     fi
     ```
   - Then create the branch:
     ```bash
     git checkout "$DEFAULT_BRANCH" && git pull origin "$DEFAULT_BRANCH"
     git checkout -b feature/TASK-<id>-brief-description
     ```

3. **Determine the best subagent(s)** based on:
   - Task domain
   - Task assignee field (often indicates the right agent type)
   - Task description and requirements

4. **Explore the codebase before implementing** — use a sub-agent to research:
   - What files will need to change?
   - Are there existing patterns to follow?
   - What tests already exist for this area?

   Report findings before writing any code.

5. **Scope check — only implement what the task describes.**
   The task's `summary` and `description` fields define the full scope of work for this session. If the description references or links to external documents (evaluation docs, design specs, RFCs), treat them as **background context only** — do not implement items from those docs that go beyond what the task's own description asks for. Referenced docs often describe multi-task plans; implementing the entire plan collapses future tasks into one PR and defeats dependency ordering.

6. **Delegate the work** to the chosen subagent(s).

7. **Create atomic commits** as you complete logical units of work.
    - All commits should be on the feature branch, NOT the default branch.
    - **After every commit, log a progress checkpoint** (see below).

8. **Log a progress checkpoint after every commit:**
    ```bash
    tusk progress <id> --next-steps "<what remains to be done>"
    ```
    The `next_steps` field is critical — write it as if briefing a new agent who has zero context. Include:
    - What has been implemented so far
    - What still needs to be done
    - Any decisions made or open questions
    - The current branch name

    **Schema migration reminder:** If the commit includes changes to `bin/tusk` that add or modify a migration (inside `cmd_migrate()`), run `tusk migrate` on the live database immediately after committing. Downstream operations in this session (retro, progress checkpoints, acceptance criteria inserts) that reference new tables or columns will fail if the live DB schema is not up to date.

9. **Review the code locally** before considering the work complete.

10. **Run convention lint (advisory)** — check for common convention violations before pushing:
    ```bash
    tusk lint
    ```
    Review the output. This check is **advisory only** — violations are warnings, not blockers. Fix any clear violations in files you've already touched. Do not refactor unrelated code just to satisfy lint.

11. **Push the branch and create a PR**:
    ```bash
    git push -u origin feature/TASK-<id>-description
    gh pr create --base "$DEFAULT_BRANCH" --title "[TASK-<id>] Brief task description" --body "..."
    ```
    Capture the PR URL from the output.

12. **Update the task with the PR URL**:
    ```bash
    tusk "UPDATE tasks SET github_pr = $(tusk sql-quote "<pr_url>"), updated_at = datetime('now') WHERE id = <id>"
    ```

13. **Review loop — iterate until approved**:

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
    4. Log a progress checkpoint (step 8) after each review-fix commit

    **Category B — Defer to backlog (cosmetic only):**
    - Pure style preferences not affecting correctness
    - Suggestions about pre-existing code NOT touched by this PR
    - Aspirational ideas about unrelated modules

    For each Category B comment:
    1. **Check for duplicates first** using `/check-dupes`:
       ```bash
       tusk dupes check "[Deferred] <brief description>" --domain <domain>
       ```
    2. Create a deferred task (with 60-day expiry):
       ```bash
       tusk "INSERT INTO tasks (summary, description, status, priority, domain, created_at, updated_at, expires_at)
         VALUES ($(tusk sql-quote "[Deferred] <brief description>"), $(tusk sql-quote "Deferred from PR #<pr_number> review for TASK-<id>.

Original comment: <comment text>

Reason deferred: <why this can wait>"), 'To Do', 'Low', '<domain>', datetime('now'), datetime('now'), datetime('now', '+60 days'))"
       ```

14. **PR approved — finalize, merge, and retro** (execute steps 14–16 as a single uninterrupted sequence — do NOT pause for user confirmation between them):

    Close the session **before** merging (captures diff stats from the feature branch, which is deleted after merge):
    ```bash
    tusk session-close $SESSION_ID
    ```

    Merge and delete the feature branch:
    ```bash
    gh pr merge $PR_NUMBER --squash --delete-branch
    ```

    Mark all acceptance criteria as done before closing the task:
    ```bash
    for cid in $(tusk "SELECT id FROM acceptance_criteria WHERE task_id = <id> AND is_completed = 0"); do
      tusk criteria done "$cid"
    done
    ```

    Update task status:
    ```bash
    tusk "UPDATE tasks SET status = 'Done', closed_reason = 'completed', updated_at = datetime('now') WHERE id = <id>"
    ```

15. **Check for newly unblocked tasks**:
    ```bash
    tusk -header -column "
    SELECT t.id, t.summary, t.priority
    FROM tasks t
    JOIN task_dependencies d ON t.id = d.task_id
    WHERE d.depends_on_id = <id> AND t.status = 'To Do'
    "
    ```

16. **Run retrospective** — mandatory, run immediately without asking. Invoke `/retro` to review the session, surface process improvements, and create any follow-up tasks. Do NOT ask "shall I run retro?" — just run it.

### Other Subcommands

For `done`, `view`, `list`, `domain`, `assignee`, `blocked`, `wip`, and `preview` subcommands, read the reference file in this skill directory:

```
Read file: <base_directory>/SUBCOMMANDS.md
```

Where `<base_directory>` is the skill base directory shown at the top of this file.

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
