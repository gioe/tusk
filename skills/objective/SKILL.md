---
name: objective
description: Plan an objective, run an existing objective plan, or perform the full objective lifecycle
allowed-tools: Bash, Task, Read, Glob, Grep
---

# Objective

Separates objective planning from execution while preserving the original
end-to-end workflow. Planning delegates task-boundary reasoning to
`/create-task` and persists the approved DAG through one objective-aware,
atomic `task-import`. Execution reuses `/chain`; this skill never reimplements
background-agent orchestration.

> Use `/create-task` for task creation — it handles decomposition, deduplication, criteria, and deps. This skill never inserts tasks directly with `tusk task-insert`; it always routes decomposition through `/create-task`.

## Arguments and modes

- `/objective plan OBJ-N` — decompose an existing active objective, persist
  and display its linked task DAG, then stop before execution.
- `/objective run OBJ-N` — execute an already-planned active objective.
- `/objective full <freeform intent>` — create, plan, run, roll up, and close.
- `/objective <freeform intent>` — backward-compatible alias for `full`.

Parse the mode before starting. `plan` and `run` require an `OBJ-N` argument.
`full` requires initiative-level intent. If no argument is given, explain the
three modes and ask for an intent to run in `full` mode.

Wait for the answer before continuing.

## Step 0: Start Cost Tracking

Record the start of this objective run so cost can be captured when it finishes. An objective spans more than one task, so cost cannot be attributed to a single task row — **omit `--task-id`** (same rule `/chain` uses for multiple heads):

```bash
tusk skill-run start objective
```

This prints `{"run_id": N, "started_at": "...", "task_id": null}`. Capture `run_id` — it's referenced by every exit path below.

> **Early-exit cleanup:** If any step below causes the skill to stop before the final report in Step 7, first call `tusk skill-run cancel <run_id>` to close the open row, then stop. Otherwise the row lingers as `(open)` in `tusk skill-run list` forever. The explicit cancel calls below cover the known early-exit paths; if you hit an unexpected bail-out, cancel before returning.

## Step 1: Resolve or Create the Objective

For `plan` or `run`, load the supplied objective:

```bash
tusk objective get <OBJECTIVE_ID>
```

Require `status=active`. Capture its summary and description as the intent.
For `run`, require at least one linked task, then skip directly to Step 4.

For `full`, distill the intent into a one-line summary and create it:

```bash
tusk objective insert "<one-line summary of the intent>" --description "<the full freeform intent>"
```

This prints `{"id": N, "summary": "...", "status": "active", ...}`. Capture `id` as `OBJECTIVE_ID` (the display form is `OBJ-<id>`). State it back to the user verbatim: `Created OBJ-<id>: <summary>`.

> The objective summary/description go through the shared shell-metacharacter guard (issue #1106) — do not embed backticks, `$(...)`, `${...}`, or bare `$IDENT` in either string; rewrite with plain words.

## Step 2: Plan Atomically via /create-task

Run `/create-task` against the objective intent in objective-planning context:

```
Read file: .claude/skills/create-task/SKILL.md
```

Pass both the intent and `OBJECTIVE_ID`. Follow `/create-task` through
decomposition, deduplication, approval, criteria, dependency planning, and its
objective-aware `task-import` materialization. It must use stable local keys,
objective relationships on every item, `duplicate_policy: "skip"`, and one
default atomic import without `--best-effort`. Capture planned task IDs from
both `created.*.task_id` and `skipped.*.matched_task_id`. Never infer task
identity from task-number order or perform post-hoc per-task link writes.

## Step 3: Verify and Display the Plan

Confirm at least one task is linked:

```bash
tusk objective get <OBJECTIVE_ID>
```

The `tasks` array must be non-empty. Display a linked-task table and the
dependency-edge DAG using the confirmed import payload or `tusk deps list`
for the linked tasks.

For `plan`, report that the objective remains active, finish the objective
skill run, and **stop here**. Do not invoke `/chain`, `/tusk`, rollup closure,
or `tusk objective done`.

For `full`, continue immediately to Step 4.

## Step 4: Execute the Linked Sub-DAG in Parallel Waves (reuse /chain)

The linked tasks are driven to Done by **reusing `/chain`'s background-agent wave machinery** — do not reimplement parallel orchestration here (this is a recorded design decision for this skill).

**4a. Determine the chain head(s).** The heads are the linked tasks that are not blocked by another *linked* task — i.e. the roots of the objective's sub-DAG. The simplest robust choice is to pass **all** linked task ids that are ready or in progress as heads; `/chain` computes the downstream sub-DAG from there and de-duplicates. Inspect dependencies if you want a tighter head set:

```bash
tusk deps ready
```

Use the linked task ids (intersected with ready/eligible tasks) as the head list `HEAD_IDS`.

The heads need **not** converge on a shared downstream task. Objectives routinely decompose into independent strands (e.g. `A->B`, `C->D`, `E` standalone), so the union of `HEAD_IDS` has no common dependent — that is a first-class shape, not an error. Pass the whole non-converging set to `/chain` in one invocation; `tusk chain scope|frontier|validate-scope` computes the union of the per-head sub-DAGs and drives every strand in the same parallel waves (issue #1133). Do **not** split disjoint strands into separate `/chain` calls.

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

- **The atomic plan resolved no task IDs** — cancel the run and leave the objective active for correction (Step 2).
- **Plan verification found no linked tasks** — surface the import result, cancel the run, and stop (Step 3).
- **Wave execution stalled or a task could not complete** — honor `/chain`'s Resume/Skip/Abort recovery; leave the objective open and report remaining work (Steps 4 and 6).
- **VERSION/CHANGELOG conflicts** — never bump from this skill; the single consolidated bump lives in `/chain`'s Step 5. If a parallel agent bumped independently and caused a conflict, resolve it down to one bump for the whole objective.
