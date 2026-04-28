---
name: investigate
description: Investigate the scope of a problem and form an honest assessment — task creation is optional
allowed-tools: Bash, Read, Glob, Grep, Task, Write, EnterPlanMode
---

# Investigate Skill

Scopes a problem through structured codebase research and forms an honest assessment. **This skill is investigation-only — it never modifies files, runs tests, or implements anything.** The investigation may conclude that no action is needed; task creation is a conditional outcome, not a guaranteed one.

## Step 0: Start Cost Tracking

Record the start of this investigation so cost can be captured at the end:

```bash
tusk skill-run start investigate
```

This prints `{"run_id": N, "started_at": "..."}`. Capture `run_id` — you will need it in Step 8.

> **Early-exit cleanup:** If any check below causes the skill to stop before Step 8 (e.g., the user never provides a problem statement, or the investigation is abandoned before the report), first call `tusk skill-run cancel <run_id>` to close the open row, then stop. Otherwise the row lingers as `(open)` in `tusk skill-run list` forever.

## Step 1: Capture the Problem

The user provides a problem statement after `/investigate`. It could be:
- A bug report or error message
- A performance concern or regression
- A design smell or architectural issue
- A feature area in need of refactoring
- A vague concern ("something feels wrong in auth")

If the user didn't provide a description, ask:

> What problem should I investigate? Describe the issue, area of concern, or question you want scoped.

If the user does not respond, or declines to provide a problem statement, run `tusk skill-run cancel <run_id>` and stop.

**Valid outcomes include "no action needed."** The goal is an honest assessment, not a task list. If investigation reveals the concern is unfounded, the code is already correct, or existing tasks cover it, say so clearly — that is a successful investigation.

## Step 2: Enter Plan Mode

Use the `EnterPlanMode` tool now. This enforces the investigation contract — no files will be written or modified during the investigation phase.

## Step 3: Defer Context Loading

Project config, backlog, and pillars are only needed when drafting remediation. Skip the fetch here — Step 5 loads them on demand, so "no action needed" flows skip the cost entirely.

## Step 4: Investigate

Use read-only tools to understand the problem. Shape the investigation around the problem statement — don't go wide for completeness, go deep where the problem points.

> Use Read/Grep/Glob and tusk Bash queries only — Plan Mode blocks writes.

**Prefer direct `Grep` + `Read` over sub-agents.** For scoped problems (a specific symbol, file, or error string), direct search is cheaper and faster than spawning an Explore agent. Default to **at most 1** `Task(subagent_type=Explore)` call for this skill, and only when the problem genuinely spans many unknown locations — overriding Plan Mode's "up to 3 in parallel" default. If you can name the files or symbols involved, skip Explore entirely.

### What to answer for each affected area

| Question | Why it matters |
|----------|----------------|
| What files/modules are affected? | Defines the scope of remediation |
| What is the root cause? | Ensures tasks fix causes, not symptoms |
| What is currently broken or missing? | Drives acceptance criteria |
| What edge cases or failure modes exist? | Surfaces what a narrow fix would miss |
| Are there related issues in nearby code? | Candidates for tangential tasks |
| Are any open backlog tasks already addressing this? | Avoids duplicating existing work |

Stop when you have a clear picture of the problem area — whether that leads to concrete remediation tasks or to the conclusion that no action is needed.

**Exhaustiveness:** Report every distinct finding the evidence supports — do not force findings into clusters to reach a round number. Artificial grouping hides signal; artificial splitting adds noise.

## Step 5: Write the Investigation Report

**Load context now** (deferred from Step 3 — skip entirely if there is nothing to remediate):

```bash
tusk setup
tusk pillars list
```

Parse `tusk setup` for `config` (domains, agents, task_types, priorities, complexity) and `backlog` (open tasks — used to catch existing coverage). `tusk pillars list` returns `[{id, name, core_claim}]` or `[]`; if empty, skip the Pillar filter below.

### Decision Criteria

A finding belongs in **Proposed Remediation** only if it passes all six filters. If it fails any filter, move it to **Out of Scope** and note which filter it failed and why. Exception: a finding that fails only the **Convention redirect** filter is kept in Proposed Remediation as an inline `tusk conventions add` action.

| Filter | Question to ask |
|--------|-----------------|
| **Pillar impact** | Does acting on this finding align with at least one project pillar? Conflicts with core design values belong out of scope regardless of severity. *(Skip if the pillars array was empty.)* |
| **Root cause vs. symptom** | Is this the root cause, or a downstream symptom of another finding already in scope? |
| **Actionability** | Can a task be written with clear, verifiable acceptance criteria? Vague concerns belong in Open Questions. |
| **Cost of inaction** | If left unfixed, does this finding cause measurable harm (data loss, user-facing breakage, security risk, compounding tech debt)? |
| **Backlog coverage** | Is an open backlog task already addressing this? If yes, note the existing task ID and exclude it. |
| **Convention redirect** | Does this finding state a rule, heuristic, or invariant that belongs in the conventions DB? If yes, do not propose a task — include the exact `tusk conventions add` command as an inline action. |

---

Prepare the report before exiting plan mode. Format:

```markdown
## Investigation: <problem title>

### Summary
One or two sentences: root cause and scope.

### Affected Areas
- `path/to/file.py` — what is wrong here

### Root Cause
Detailed explanation. Include relevant code snippets inline.

### Proposed Remediation *(omit if nothing actionable)*

> Zero tasks is a valid outcome. Only include tasks that passed all six Decision Criteria filters, plus convention redirects.

**<imperative summary>** (Priority · Domain · Type · Complexity)
> What needs to be done and why. Include acceptance criteria ideas.

**Convention redirect: <one-line description>**
> `tusk conventions add --topic <topic> --text "<rule>" --source investigate`

### Out of Scope
Related issues that did not pass the Decision Criteria filters. Note which filter each failed.

### Open Questions
Ambiguities or decisions that need input before work can begin. Omit if none.
```

## Step 6: Exit Plan Mode

Use `ExitPlanMode` to present the investigation report for user review. Set `allowedPrompts` to allow only task creation — no implementation:

```json
[{"tool": "Bash", "prompt": "run /create-task to create tasks if the user approves"}]
```

After presenting the report, ask the user:

> Should I create tasks for the proposed remediation?

Wait for the user to respond. They may ask follow-ups, request a deeper look (re-investigate only if genuinely new ground is needed), remove specific tasks, or decline entirely.

## Step 7: Hand Off to /create-task *(conditional — skip if user declined)*

If the user approved any Proposed Remediation items, invoke `/create-task` via the Skill tool with the approved items as the payload:

```
Skill(skill="create-task", args="<approved remediation items>")
```

`/create-task` handles decomposition review, acceptance criteria generation, duplicate detection, metadata assignment, and dependency proposals.

Track the total number of tasks created from the `/create-task` results — you will need it in Step 8.

## Step 8: Finish Cost Tracking

Record cost for this investigation run. Replace `<run_id>` with the value captured in Step 0, `<N>` with the number of tasks proposed in the report, and `<M>` with the total number of tasks created in Step 7 (0 if the step was skipped).

```bash
tusk skill-run finish <run_id> --metadata '{"tasks_proposed":<N>,"tasks_created":<M>}'
```

This reads the Claude Code transcript for the time window of this run and stores token counts, estimated cost, and productivity metadata in the `skill_runs` table. The captured window covers the full session — investigation plus `/create-task` — so the reported cost reflects the entire `/investigate` invocation.

To view cost history:

```bash
tusk skill-run list investigate
```
