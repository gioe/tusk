---
name: resume-task
description: Resume work on a task after a session crash or timeout
allowed-tools: Bash, Task, Read, Edit, Write, Grep, Glob
---

# Resume Task Skill

Automates recovery after a Claude Code session crash or timeout. Detects the current task from the branch name, gathers all context (task details, progress checkpoints, acceptance criteria, recent commits), displays a recovery summary, and continues the implementation workflow.

## Step 1: Detect the Task ID

Extract the task ID from the current git branch name:

```bash
BRANCH=$(git branch --show-current)
TASK_ID=$(echo "$BRANCH" | sed -n 's|^feature/TASK-\([0-9]*\)-.*|\1|p')
echo "Branch: $BRANCH"
echo "Task ID: $TASK_ID"
```

- If `TASK_ID` is non-empty, proceed to Step 2.
- If the branch does not match `feature/TASK-<id>-...`, check whether the user provided a task ID as an argument (e.g., `/resume-task 42`). If so, use that.
- If neither the branch nor an argument provides a task ID, ask the user: "Could not detect a task ID from the current branch. Which task ID should I resume?"

## Step 2: Start the Task (Idempotent)

Fetch task details, reuse the existing session, and gather progress history:

```bash
tusk task-start <TASK_ID>
```

This returns JSON with three keys:
- `task` — full task row (summary, description, priority, domain, assignee, complexity, etc.)
- `progress` — array of prior progress checkpoints (most recent first). The first entry's `next_steps` tells you exactly where to pick up.
- `session_id` — reuses an open session if one exists

Hold onto `session_id` for later use.

## Step 3: Gather Context

Run these in parallel to collect recovery context:

```bash
tusk criteria list <TASK_ID>
```

```bash
git log --oneline $(git merge-base HEAD main)..HEAD
```

## Step 4: Display Recovery Summary

Present all gathered context in a clear recovery summary:

**Task:** `[TASK-<id>] <summary>` (priority: `<priority>`, complexity: `<complexity>`, domain: `<domain>`)

**Description:** `<description>`

**Progress Checkpoints:** (most recent first)
- Show each checkpoint's `next_steps`, `commit_hash`, and `files_changed`
- If no checkpoints exist, note "No prior progress checkpoints found."

**Acceptance Criteria:**
- Show each criterion with its completion status (done/pending)
- Highlight incomplete criteria — these define what remains

**Recent Commits on This Branch:**
- Show the git log output from Step 3

**Next Steps:** Quote the most recent checkpoint's `next_steps` field prominently — this is the primary guide for what to do next. If no checkpoints exist, use the incomplete acceptance criteria as the guide.

## Step 5: Resume the /next-task Workflow

Continue the `/next-task` implementation workflow from **step 4 onward** (determining subagents, exploring the codebase, implementing, committing, and marking criteria done). Skip steps 1-3 since the task is already started and the branch already exists.

Key reminders for the resumed workflow:
- Mark each acceptance criterion done (`tusk criteria done <cid>`) as you complete it
- Log progress checkpoints after each commit:
  ```bash
  tusk progress <TASK_ID> --next-steps "<what remains to be done>"
  ```
- Run `tusk lint` before pushing (advisory only)
- For push, PR, review, merge, and retro steps, read the companion file:
  ```
  Read file: <next_task_base>/FINALIZE.md
  ```
  Where `<next_task_base>` is the base directory of the `next-task` skill (sibling to this skill's directory).
