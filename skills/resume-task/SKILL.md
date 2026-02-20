---
name: resume-task
description: Resume work on a task after a session crash or timeout
allowed-tools: Bash, Task, Read, Edit, Write, Grep, Glob
---

# Resume Task Skill

Recovers context after a session crash/timeout and continues the implementation workflow.

## Step 1: Detect the Task ID

```bash
BRANCH=$(git branch --show-current)
TASK_ID=$(echo "$BRANCH" | sed -n 's|^feature/TASK-\([0-9]*\)-.*|\1|p')
echo "Branch: $BRANCH"
echo "Task ID: $TASK_ID"
```

- Non-empty `TASK_ID` → proceed to Step 2
- Branch doesn't match → check for user-provided argument (e.g., `/resume-task 42`)
- Neither → ask: "Could not detect a task ID. Which task ID should I resume?"

## Step 2: Start the Task (Idempotent)

```bash
tusk task-start <TASK_ID>
```

Returns JSON with four keys:

```
task        — full task row (summary, description, priority, domain, assignee, complexity)
progress    — checkpoints (most recent first); first entry's next_steps = resume point
criteria    — acceptance criteria (id, criterion, source, is_completed)
session_id  — reuses open session if one exists
```

Hold onto `session_id` for later use.

## Step 3: Gather Context

```bash
git log --oneline $(git merge-base HEAD main)..HEAD
```

## Step 4: Display Recovery Summary

```
Task:        [TASK-<id>] <summary> (priority, complexity, domain)
Description: <description>

Progress Checkpoints: (most recent first)
  - <next_steps> | <commit_hash> | <files_changed>
  (or "No prior checkpoints found.")

Acceptance Criteria:
  - [x] completed criterion
  - [ ] pending criterion  ← defines remaining work

Recent Commits: (git log output from Step 3)

Next Steps: <most recent checkpoint's next_steps, or incomplete criteria if none>
```

## Step 5: Resume the /next-task Workflow

Continue from `/next-task` **step 4 onward** (subagents → explore → implement → commit → criteria → finalize). Steps 1-3 are already done.

- Mark criteria done as you go: `tusk criteria done <cid>`
- Log progress after each commit:
  ```bash
  tusk progress <TASK_ID> --next-steps "<what remains>"
  ```
- Run `tusk lint` before pushing (advisory only)
- For finalize steps, read:
  ```
  Read file: <next_task_base>/FINALIZE.md
  ```
  Where `<next_task_base>` is the `next-task` skill's base directory (sibling to this skill).
