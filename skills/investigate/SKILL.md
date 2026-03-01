---
name: investigate
description: Investigate the scope of a problem and propose remediation tasks — no implementation
allowed-tools: Bash, Read, Glob, Grep, Task, Write, EnterPlanMode
---

# Investigate Skill

Scopes a problem through structured codebase research, then proposes concrete remediation tasks for later work. **This skill is investigation-only — it never modifies files, runs tests, or implements anything.** All output feeds into `/create-task`.

## Step 0: Start Cost Tracking

Record the start of this investigation so cost can be captured at the end:

```bash
tusk skill-run start investigate
```

This prints `{"run_id": N, "started_at": "..."}`. Capture `run_id` — you will need it in Step 8.

## Step 1: Capture the Problem

The user provides a problem statement after `/investigate`. It could be:
- A bug report or error message
- A performance concern or regression
- A design smell or architectural issue
- A feature area in need of refactoring
- A vague concern ("something feels wrong in auth")

If the user didn't provide a description, ask:

> What problem should I investigate? Describe the issue, area of concern, or question you want scoped.

## Step 2: Enter Plan Mode

Use the `EnterPlanMode` tool now. This enforces the investigation contract — no files will be written or modified during the investigation phase.

## Step 3: Load Project Context

Fetch config and open backlog for reference:

```bash
tusk setup
```

Parse the returned JSON. Hold `config` (domains, agents, task_types, priorities, complexity) and `backlog` (open tasks) in context. You'll need both when proposing remediation tasks — `backlog` lets you catch tasks that already cover the same ground.

## Step 4: Investigate

Use read-only tools to understand the problem. Shape the investigation around the problem statement — don't go wide for completeness, go deep where the problem points.

**Allowed tools during investigation:**
- `Read` — read source files, configs, tests, docs
- `Glob` — discover files in relevant directories
- `Grep` — search for patterns, symbols, error strings, function calls
- `Task` with `subagent_type=Explore` — broad multi-location searches where simple grep is insufficient
- `Bash` — run `tusk` commands only (queries to the task database, not build/test commands)

**Prohibited during investigation:**
- `Write`, `Edit` — do not touch any project files
- Build commands, test runners, scripts that produce side effects

### What to answer for each affected area

| Question | Why it matters |
|----------|----------------|
| What files/modules are affected? | Defines the scope of remediation |
| What is the root cause? | Ensures tasks fix causes, not symptoms |
| What is currently broken or missing? | Drives acceptance criteria |
| What edge cases or failure modes exist? | Surfaces what a narrow fix would miss |
| Are there related issues in nearby code? | Candidates for tangential tasks |
| Are any open backlog tasks already addressing this? | Avoids duplicating existing work |

Stop when you have enough to propose concrete, actionable tasks.

## Step 5: Write the Investigation Report

Prepare the report before exiting plan mode. Format:

```markdown
## Investigation: <problem title>

### Summary
One or two sentences: root cause and scope.

### Affected Areas
- `path/to/file.py` — what is wrong here
- `path/to/other.ts` — what is wrong here

### Root Cause
Detailed explanation. Include relevant code snippets inline (do not re-read files at this stage).

### Proposed Remediation

**Task 1: <imperative summary>** (Priority · Domain · Type · Complexity)
> What needs to be done and why. Include 2–3 acceptance criteria ideas.

**Task 2: <imperative summary>** (Priority · Domain · Type · Complexity)
> ...

### Out of Scope
Related issues discovered that should not be part of this remediation. Candidates for separate tasks or future work.

### Open Questions
Ambiguities or decisions that need input before work can begin. Omit this section if none.
```

## Step 6: Exit Plan Mode

Use `ExitPlanMode` to present the investigation report for user review. Set `allowedPrompts` to allow only task creation — no implementation:

```json
[{"tool": "Bash", "prompt": "run tusk task-insert commands to create tasks"}]
```

Wait for the user to review. They may:
- Ask follow-up questions → answer from your investigation findings; re-investigate only if genuinely new ground is needed
- Request a deeper look at a specific area → use read-only tools, then update the report
- Approve the proposed tasks → proceed to Step 7
- Remove specific tasks → exclude them from Step 7
- Decide not to create tasks → end the skill gracefully

## Step 7: Create Tasks via /create-task

> **Note:** Keep the `run_id` from Step 0 in context — you will need it after `/create-task` completes in Step 8.

Once the user approves, pass the proposed remediation tasks to the `/create-task` workflow. Read the skill:

```
Read file: <base_directory>/../create-task/SKILL.md
```

Follow its instructions from **Step 1**, using the Proposed Remediation section from your report as the input text. `/create-task` will handle:
- Decomposition review and user approval
- Acceptance criteria generation
- Duplicate detection
- Metadata assignment (priority, domain, task_type, complexity, assignee)
- Dependency proposals

## Step 8: Finish Cost Tracking

Record cost for this investigation run. Replace `<run_id>` with the value captured in Step 0, `<N>` with the number of tasks proposed in your Investigation Report (Step 5), and `<M>` with the number of tasks actually created by `/create-task` (Step 7). If the user declined to create tasks, set `<M>` to 0.

```bash
tusk skill-run finish <run_id> --metadata '{"tasks_proposed":<N>,"tasks_created":<M>}'
```

This reads the Claude Code transcript for the time window of this run and stores token counts, estimated cost, and productivity metadata in the `skill_runs` table. Note that the captured window covers the full session — including both the investigation phase and the `/create-task` workflow — so the reported cost reflects the entire `/investigate` invocation.

To view cost history across all investigate runs:

```bash
tusk skill-run list investigate
```

## Hard Constraints

- **Never write or edit project files** — not during investigation, not after
- **Never run build commands, test runners, or scripts** — `Bash` is for `tusk` queries only
- **Never implement any proposed task** — that is the job of `/tusk`
- **Never insert tasks directly** with `tusk task-insert` — always hand off to `/create-task`
- If the fix is trivially obvious (a one-line typo, obvious config error), note it in the report as a "Trivial Fix" and let the user decide whether to create a task or fix it manually — do not apply it yourself
