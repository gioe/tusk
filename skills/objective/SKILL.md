---
name: objective
description: Run an objective end to end — create it, decompose into tasks via /create-task, execute the linked sub-DAG in parallel waves via /chain, roll up, and close
allowed-tools: Bash, Task, Read, Glob, Grep
---

# Objective

Runs the full objective lifecycle from a single freeform intent: creates an objective, hands the intent to `/create-task` for decomposition, links each created task to the objective, drives the linked tasks' dependency sub-DAG to Done in parallel background-agent waves by **reusing `/chain`'s wave-execution machinery** (never reimplementing background-agent orchestration), reads `tusk objective brief` for the aggregate picture, summarizes, decides next steps, and closes the objective.

> Use `/create-task` for task creation — it handles decomposition, deduplication, criteria, and deps. This skill never inserts tasks directly with `tusk task-insert`; it always routes decomposition through `/create-task`.

## Arguments

`/objective <freeform intent describing the larger goal>`

The argument is the initiative-level intent — a paragraph or a few sentences describing the larger goal that spans more than one shippable task. If no argument is given, prompt the user:

> What objective would you like to run? Describe the larger goal — I'll decompose it into tasks, execute them in parallel, and close the objective when they're done.

Wait for the answer before continuing.

## Step 0: Start Cost Tracking

Record the start of this objective run so cost can be captured when it finishes. An objective spans more than one task, so cost cannot be attributed to a single task row — **omit `--task-id`** (same rule `/chain` uses for multiple heads):

```bash
tusk skill-run start objective
```

This prints `{"run_id": N, "started_at": "...", "task_id": null}`. Capture `run_id` — it's referenced by every exit path below.

> **Early-exit cleanup:** If any step below causes the skill to stop before the final report in Step 7, first call `tusk skill-run cancel <run_id>` to close the open row, then stop. Otherwise the row lingers as `(open)` in `tusk skill-run list` forever. The explicit cancel calls below cover the known early-exit paths; if you hit an unexpected bail-out, cancel before returning.

## Step 1: Create the Objective

Distill the intent into a one-line summary and create the objective:

```bash
tusk objective insert "<one-line summary of the intent>" --description "<the full freeform intent>"
```

This prints `{"id": N, "summary": "...", "status": "active", ...}`. Capture `id` as `OBJECTIVE_ID` (the display form is `OBJ-<id>`). State it back to the user verbatim: `Created OBJ-<id>: <summary>`.

> The objective summary/description go through the shared shell-metacharacter guard (issue #1106) — do not embed backticks, `$(...)`, `${...}`, or bare `$IDENT` in either string; rewrite with plain words.

## Step 2: Decompose the Intent via /create-task

`/create-task` is the only task-creation path. Before handing off, snapshot the current max task id so the tasks it creates can be discovered reliably regardless of how many it produces:

```bash
BEFORE_MAX=$(tusk "SELECT COALESCE(MAX(id), 0) FROM tasks")
```

Then run `/create-task` against the **same intent**, following its instructions inline:

```
Read file: .claude/skills/create-task/SKILL.md
```

Pass the objective's freeform intent as the `/create-task` input and follow that skill's steps (decomposition, dedup, criteria, deps) to completion. `/create-task` may create one task or several, and may dedup some findings into existing tasks — that is expected.

After `/create-task` finishes, discover the newly-created tasks by id window:

```bash
tusk -header -column "SELECT id, summary, status, complexity FROM tasks WHERE id > $BEFORE_MAX AND status = 'To Do' ORDER BY id"
```

Store these as `NEW_TASK_IDS`. If `/create-task` deduped the entire intent into existing backlog tasks (no new rows), ask the user whether to link those pre-existing tasks to the objective instead — capture their ids as `NEW_TASK_IDS` if they agree. If there are still **zero** tasks to link, the objective has nothing to execute: run `tusk skill-run cancel <run_id>`, tell the user the intent did not decompose into any tasks, and stop (leave the empty objective for the user to populate or close manually).

## Step 3: Link the Tasks to the Objective

Link every task in `NEW_TASK_IDS` to the objective. Choose `relationship_type` per task:

- **`primary`** — the single most central deliverable for the objective (the task the objective is really about). Pick exactly one when there is a clear lead deliverable; otherwise skip `primary`.
- **`contributes_to`** — the default for supporting tasks that advance the objective.
- **`follow_up`** — a task explicitly framed as cleanup or a deferred follow-on rather than core work.

```bash
tusk objective link <OBJECTIVE_ID> <task_id> --type primary|contributes_to|follow_up
```

Run one `objective link` per task (serialize these calls — do not run tusk DB-write commands in parallel). After linking, confirm at least one task is linked:

```bash
tusk objective get <OBJECTIVE_ID>
```

The `tasks` array must be non-empty (acceptance criterion: "an objective with at least one linked task"). If it is empty, linking failed — surface the error, run `tusk skill-run cancel <run_id>`, and stop.

## Step 4: Execute the Linked Sub-DAG in Parallel Waves (reuse /chain)

The linked tasks are driven to Done by **reusing `/chain`'s background-agent wave machinery** — do not reimplement parallel orchestration here (this is a recorded design decision for this skill).

**4a. Determine the chain head(s).** The heads are the linked tasks that are not blocked by another *linked* task — i.e. the roots of the objective's sub-DAG. The simplest robust choice is to pass **all** linked task ids that are ready or in progress as heads; `/chain` computes the downstream sub-DAG from there and de-duplicates. Inspect dependencies if you want a tighter head set:

```bash
tusk deps ready
```

Use the linked task ids (intersected with ready/eligible tasks) as the head list `HEAD_IDS`.

**4b. Branch on shape:**

- **Multiple linked tasks, or a single task with downstream dependents** → hand `HEAD_IDS` to `/chain`. Follow its instructions inline:

  ```
  Read file: .claude/skills/chain/SKILL.md
  ```

  Execute `/chain`'s Steps 1–7 for `HEAD_IDS`. `/chain` spawns the parallel waves, and — critically — its **Step 5 consolidates a single VERSION & CHANGELOG bump for the whole sub-DAG** and its Step 6 runs the post-chain retro. **Collect the agent output file paths `/chain` reports** during its waves; you will read those conclusions in Step 5. Do not pause between `/objective` and `/chain` for user confirmation — drive straight through.

- **Exactly one linked task with no downstream** → `/chain` will report `no-downstream` and suggest `/tusk` instead. In that degenerate case, dispatch `/tusk <task_id>` for that single task (a one-task objective has no wave to parallelize):

  ```
  Read file: .claude/skills/tusk/SKILL.md
  ```

  Begin work on that task id and drive it to Done.

**Do not bump VERSION or CHANGELOG yourself.** Parallel agents that each bump independently collide on merge — the single post-run consolidation is delegated to `/chain`'s Step 5 (or, in the one-task fallback, handled inside `/tusk`'s own flow). This is the whole reason execution is delegated rather than reimplemented.

**If execution stalls or a wave fails**, honor `/chain`'s recovery prompts (Resume / Skip / Abort). If the objective cannot be completed, do not close it — jump to Step 6's "incomplete" branch and report what remains.

## Step 5: Roll Up the Objective

Read the aggregate picture from the read view shipped for this purpose:

```bash
tusk objective brief <OBJECTIVE_ID> --format markdown
```

This renders the status breakdown across linked tasks, criteria coverage, summed cost/duration (counted per distinct task — no double-count), and any open objective-scoped context. Show the markdown block to the user verbatim.

Then synthesize the subagent conclusions: read each agent output file path you collected in Step 4 and capture each task's final message (what shipped, any caveats). Combine the brief's quantitative rollup with these qualitative conclusions into a short summary:

- What the objective set out to do.
- Which linked tasks reached Done, and what each shipped (one line each, from the agent conclusions).
- Any tasks that did not complete, with current status.
- Total cost/duration from the brief.

## Step 6: Decide Next Steps and Close the Objective

Decide from the Step 5 rollup:

- **All linked tasks Done and the goal is met** → close the objective as completed:
  ```bash
  tusk objective done <OBJECTIVE_ID> --reason completed
  ```

- **Execution showed the objective should not be pursued** (the work proved unnecessary, wrong, or out of scope) → close it as abandoned, and say why:
  ```bash
  tusk objective done <OBJECTIVE_ID> --reason abandoned
  ```
  `tusk objective done` closes the objective's own status only — it never changes the status of linked tasks, which remain the independent shippable unit.

- **Some linked tasks remain incomplete** → do **not** close the objective. Report exactly which tasks remain and their status, and tell the user how to resume (re-run `/objective` is not needed — the objective and its links persist; re-run `/chain <head_ids>` or `/tusk <id>` for the stragglers, then re-run this skill from Step 5 to roll up and close).

## Step 7: Final Report and Finish Cost Tracking

Print the final report:

- `OBJ-<id>` summary and final objective status (`completed` / `abandoned` / still `active`).
- Linked-task outcome table (id, summary, final status).
- The cost/duration totals from the Step 5 brief.
- Any newly unblocked backlog tasks `/chain` surfaced.

Then close out the skill-run so its cost is captured:

```bash
tusk skill-run finish <run_id>
```

## Error Handling

- **`/create-task` produced no new tasks** — it deduped the whole intent into existing tasks; offer to link those instead, else cancel the run and stop (Step 2).
- **Linking failed / objective has no linked tasks** — surface the error, cancel the run, stop (Step 3).
- **Wave execution stalled or a task could not complete** — honor `/chain`'s Resume/Skip/Abort recovery; leave the objective open and report remaining work (Steps 4 and 6).
- **VERSION/CHANGELOG conflicts** — never bump from this skill; the single consolidated bump lives in `/chain`'s Step 5. If a parallel agent bumped independently and caused a conflict, resolve it down to one bump for the whole objective.
