---
name: resume-task
description: Resume work on a task after a session crash or timeout
allowed-tools: Bash, Task, Read, Edit, Write, Grep, Glob
---

# Resume Task Skill

Recovers context after a session crash/timeout and continues the implementation workflow.

## Provenance handoff

After Step 2, recover durable intent before reading code:

```bash
tusk task-brief <task-id>
tusk context list <task-id> --status active
tusk provenance register task <task-id>
tusk provenance links <task-ref> --direction outgoing --limit 20
tusk provenance get <source-ref>
tusk provenance evidence --criterion-id <criterion-id> --limit 20
```

The brief supplies criteria, dependencies, progress, and current context; this
workflow does not assume the brief already embeds provenance. Register returns
a stable task reference but invents no source links. Follow only sources linked
to this task or the selected criterion/active context atom. Use `register context`
and `register criterion` for those endpoints when needed, then `links`/`get`.
Keep a visited-reference set, at most two hops and 20 edges per lookup; surface
`truncated` rather than claiming complete recovery. Do not scan all prompts or
load a transcript. Empty links mean unknown source, not the newest message.

Include active decisions, unresolved questions, source references, and exact
proof targets in the recovery summary. Evidence history can contain old targets,
pending attempts, failures, bypasses, and declarations: compare the checked
artifact to the current deliverable before relying on it. Missing proof calls
for fresh verification. Preserve superseded decisions as history; read inactive
context only when an explicit supersession/source link makes it relevant.
Use the session returned by this resume, not stale IDs from progress text.

### Attribution boundaries

Use the current checkout's Tusk wrapper. Keep IDs returned by the current
operation; never select the latest global prompt, session, or skill run.
For each covered mutation, pass only known, matching execution identities as
command-local environment variables: `TUSK_ACTION_TASK_ID`,
`TUSK_ACTION_SESSION_ID`, `TUSK_ACTION_SKILL_RUN_ID`,
`TUSK_ACTION_WORKSPACE_ID`, and `TUSK_ACTION_SOURCE_REF`. Omit unknown values;
do not export them across tasks. A source reference is the actual source for
that operation, not automatically the task's original prompt. Nested review or
retro runs use their own returned skill-run ID. When creating a different task,
omit incompatible session/workspace IDs. Retain returned `receipt_refs`; recover
lost responses with `tusk provenance receipts --task-id <id> --limit 20`.
The CLI supplies mechanical receipts; agents supply only justified semantic
links. A receipt proves a mutation committed, not that verification passed.

Missing sources stay unknown and do not block legacy tasks. Check
`tusk provenance --help` once if capability is uncertain; on an older CLI,
continue the existing context/progress workflow and report the missing feature.
Do not fabricate historical captures, infer causation from timestamps, or read
whole transcripts to fill gaps. Supersession links alone do not retire context.


## Step 1: Detect the Task ID

```bash
tusk branch-parse
```

- Returns `{"task_id": N}` → use that ID and proceed to Step 2
- Exits 1 (branch doesn't match) → check for user-provided argument (e.g., `/resume-task 42`)
- Neither → ask: "Could not detect a task ID. Which task ID should I resume?"

## Step 2: Start the Task (Idempotent)

```bash
tusk task-start <TASK_ID> --force --force-session
```

The `--force` flag ensures the workflow proceeds even if the task has no acceptance criteria (emits a warning rather than hard-failing). The `--force-session` flag explicitly reuses an active session when resuming from outside the recorded task workspace.

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

## Step 5: Resume the /tusk Workflow

Continue from `/tusk` **step 4 onward** (subagents → explore → implement → commit → criteria → finalize). Steps 1-3 are already done.

- Mark criteria done as you go: `tusk criteria done <cid>`
- Log progress after each commit:
  ```bash
  tusk progress <TASK_ID> --next-steps "<what remains>"
  ```
- Run `tusk lint` before pushing (advisory only)
- For finalize steps (step 12), read step 12 directly from the tusk skill:
  ```
  Read file: <base_directory>/../tusk/SKILL.md
  ```
  Where `<base_directory>` is the resume-task skill's base directory.
