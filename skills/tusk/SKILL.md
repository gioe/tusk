---
name: tusk
description: Get the most important task that is ready to be worked on
allowed-tools: Bash, Task, Read, Edit, Write, Grep, Glob
---

# Tusk Skill

The primary interface for working with tasks from the project task database (via `tusk` CLI). Use this to get the next task, start working on it, and manage the full development workflow.

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
tusk -header -column "
SELECT id, summary, priority, priority_score, domain, assignee, complexity, description
FROM v_ready_tasks
ORDER BY priority_score DESC, id
LIMIT 1;
"
```

**Empty backlog**: If the query returns no rows, the backlog has no ready tasks. Check why:

```bash
tusk -header -column "SELECT status, COUNT(*) as count FROM tasks GROUP BY status"
```

- If there are **no tasks at all** (or all are Done): inform the user the backlog is empty and suggest running `/create-task` to add new work.
- If there are **To Do tasks but all are blocked**: inform the user and suggest running `/tusk blocked` to see what's holding them up.
- If there are **In Progress tasks**: inform the user and suggest running `/tusk wip` to check on active work.

Do **not** suggest `/groom-backlog` or `/retro` when there are no ready tasks — those skills require an active backlog or session history to be useful.

**Complexity warning**: If the selected task has complexity **L** or **XL**, display a warning to the user before proceeding:

> **Note: This is a large task (complexity: L/XL) — expect 3+ sessions to complete.**

Then ask the user whether to proceed or request a smaller task. If the user chooses a smaller task, re-run the query excluding L and XL:

Re-run the query above, adding `WHERE complexity NOT IN ('L', 'XL')` to filter by complexity.

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

   Hold onto `session_id` from the JSON — it will be used to close the session when the task is done.

2. **Create a new git branch IMMEDIATELY** (skip if resuming and branch already exists):
   ```bash
   tusk branch <id> <brief-description-slug>
   ```
   This detects the default branch (remote HEAD → gh fallback → main), checks it out, pulls latest, and creates `feature/TASK-<id>-<slug>`. It prints the created branch name on success.

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

7. **Implement, commit, and mark criteria done.** Work through the acceptance criteria from step 1 as your checklist — **one commit per criterion**. For each criterion in order:
    1. Implement the changes that satisfy it
    2. Commit and mark the criterion done atomically using `tusk commit --criteria`:
       ```bash
       tusk commit <id> "<message>" <file1> [file2 ...] --criteria <cid1> [<cid2> ...]
       ```
       This runs `tusk lint` (advisory — never blocks), stages the listed files, commits with the `[TASK-<id>] <message>` format and Co-Authored-By trailer, and marks each criterion done — all in one call. Each criterion is bound to the new commit hash automatically. Multiple criterion IDs can be passed space-separated after a single `--criteria` flag.
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
      After `/review-commits` completes with verdict **APPROVED**, proceed to step 12. If verdict is **CHANGES REMAINING**, surface the unresolved items to the user and stop.

12. **Finalize — merge, push, and run retro.** Execute as a single uninterrupted sequence — do NOT pause for user confirmation between steps:
    ```bash
    tusk merge <id> --session $SESSION_ID
    ```
    `tusk merge` closes the session, merges the feature branch into the default branch, pushes, deletes the feature branch, and marks the task Done. It returns JSON including an `unblocked_tasks` array. If there are newly unblocked tasks, note them in the retro.

    Then run `/retro` immediately — do not ask "shall I run retro?". Invoke it to review the session, surface process improvements, and create follow-up tasks.

### Other Subcommands

If the user invoked a subcommand (e.g., `/tusk done`, `/tusk list`, `/tusk blocked`), read the reference file:

```
Read file: <base_directory>/SUBCOMMANDS.md
```

Skip this section when running the default workflow (no subcommand argument).

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
