# Objective — Run an Objective End to End (Codex)

Runs the full objective lifecycle from a single freeform intent: creates an
objective, hands the intent to `create-task.md` for decomposition, links each
created task to the objective, drives the linked tasks' dependency sub-DAG to
Done, reads `tusk objective brief` for the aggregate picture, summarizes,
decides next steps, and closes the objective.

> **Conventions:** Run `tusk conventions search <topic>` for project rules.
> Do not restate convention text inline — it drifts from the DB.

> **Sequential execution — no parallel sub-agents.** Codex has no Task tool
> for spawning background agents. The objective's linked tasks run
> **sequentially in the current Codex session**, one at a time, by delegating
> to `chain.md` (its Codex variant is itself sequential). Do not attempt to
> launch parallel Codex sessions, background processes, or worktree splits
> from within this prompt. The tradeoff: an objective takes longer end-to-end
> than the Claude Code parallel-wave variant, but execution is deterministic
> and never produces merge conflicts between sibling tasks.

> Use `create-task.md` for task creation — it handles decomposition,
> deduplication, criteria, and deps. This prompt never inserts tasks directly
> with `tusk task-insert`; it always routes decomposition through
> `create-task.md`.

## Arguments

`/objective <freeform intent describing the larger goal>`

The argument is the initiative-level intent — a paragraph or a few sentences
describing the larger goal that spans more than one shippable task. If no
argument is given, prompt the user:

> What objective would you like to run? Describe the larger goal — I'll
> decompose it into tasks, execute them one at a time, and close the objective
> when they're done.

Wait for the answer before continuing.

## Step 0: Start Cost Tracking

Record the start of this objective run so cost can be captured when it
finishes. An objective spans more than one task, so cost cannot be attributed
to a single task row — **omit `--task-id`** (same rule `chain.md` uses for
multiple heads):

```bash
tusk skill-run start objective
```

This prints `{"run_id": N, "started_at": "...", "task_id": null}`. Capture
`run_id` — it's referenced by every exit path below.

> **Early-exit cleanup:** If any step below causes the prompt to stop before
> the final report in Step 7, first call `tusk skill-run cancel <run_id>` to
> close the open row, then stop. Otherwise the row lingers as `(open)` in
> `tusk skill-run list` forever.

## Step 1: Create the Objective

Distill the intent into a one-line summary and create the objective:

```bash
tusk objective insert "<one-line summary of the intent>" --description "<the full freeform intent>"
```

This prints `{"id": N, "summary": "...", "status": "active", ...}`. Capture
`id` as `OBJECTIVE_ID` (the display form is `OBJ-<id>`). State it back to the
user verbatim: `Created OBJ-<id>: <summary>`.

> The objective summary/description go through the shared
> shell-metacharacter guard — do not embed backticks, `$(...)`, `${...}`, or
> bare `$IDENT` in either string; rewrite with plain words.

## Step 2: Decompose the Intent via create-task.md

`create-task.md` is the only task-creation path. Before handing off, snapshot
the current max task id so the tasks it creates can be discovered reliably
regardless of how many it produces:

```bash
BEFORE_MAX=$(tusk "SELECT COALESCE(MAX(id), 0) FROM tasks")
```

Then run `create-task.md` against the **same intent**, passing the objective's
freeform intent as its input and following that prompt's steps (decomposition,
dedup, criteria, deps) to completion. `create-task.md` may create one task or
several, and may dedup some findings into existing tasks — that is expected.

After `create-task.md` finishes, discover the newly-created tasks by id window:

```bash
tusk -header -column "SELECT id, summary, status, complexity FROM tasks WHERE id > $BEFORE_MAX AND status = 'To Do' ORDER BY id"
```

Store these as `NEW_TASK_IDS`. If `create-task.md` deduped the entire intent
into existing backlog tasks (no new rows), ask the user whether to link those
pre-existing tasks to the objective instead — capture their ids as
`NEW_TASK_IDS` if they agree. If there are still **zero** tasks to link, the
objective has nothing to execute: run `tusk skill-run cancel <run_id>`, tell
the user the intent did not decompose into any tasks, and stop (leave the empty
objective for the user to populate or close manually).

## Step 3: Link the Tasks to the Objective

Link every task in `NEW_TASK_IDS` to the objective. Choose `relationship_type`
per task:

- **`primary`** — the single most central deliverable for the objective. Pick
  exactly one when there is a clear lead deliverable; otherwise skip `primary`.
- **`contributes_to`** — the default for supporting tasks that advance the
  objective.
- **`follow_up`** — a task explicitly framed as cleanup or a deferred
  follow-on rather than core work.

```bash
tusk objective link <OBJECTIVE_ID> <task_id> --type primary|contributes_to|follow_up
```

Run one `objective link` per task (serialize these calls — do not run tusk
DB-write commands in parallel). After linking, confirm at least one task is
linked:

```bash
tusk objective get <OBJECTIVE_ID>
```

The `tasks` array must be non-empty (an objective with at least one linked
task). If it is empty, linking failed — surface the error, run
`tusk skill-run cancel <run_id>`, and stop.

## Step 4: Execute the Linked Sub-DAG Sequentially (delegate to chain.md)

The linked tasks are driven to Done by delegating to `chain.md`, whose Codex
variant runs the sub-DAG **sequentially in this session** — one task at a time.
Do not reimplement task execution here.

**4a. Determine the chain head(s).** The heads are the linked tasks that are
not blocked by another *linked* task — i.e. the roots of the objective's
sub-DAG. The simplest robust choice is to pass **all** linked task ids that are
ready or in progress as heads; `chain.md` computes the downstream sub-DAG from
there and de-duplicates. Inspect dependencies if you want a tighter head set:

```bash
tusk deps ready
```

Use the linked task ids (intersected with ready/eligible tasks) as the head
list `HEAD_IDS`.

**4b. Branch on shape:**

- **Multiple linked tasks, or a single task with downstream dependents** →
  hand `HEAD_IDS` to `chain.md` and follow its Steps 1–7. `chain.md` walks each
  head and frontier task sequentially via `tusk.md`, and — critically — its
  **Step 5 consolidates a single VERSION & CHANGELOG bump for the whole
  sub-DAG**, and its Step 6 runs the post-chain retro. Capture each task's
  conclusion as it completes (in sequential mode you observe each `tusk merge`
  result directly in this session); you will summarize them in Step 5.

- **Exactly one linked task with no downstream** → `chain.md` reports
  `no-downstream` and suggests `tusk.md` instead. In that degenerate case,
  follow `tusk.md` Step 1 onward for that single task id and drive it to Done.

**Do not bump VERSION or CHANGELOG yourself.** The single consolidated bump is
delegated to `chain.md`'s Step 5 (or, in the one-task fallback, handled inside
`tusk.md`'s own flow). Even though Codex execution is already sequential and
conflict-free, the consolidation keeps one bump per objective rather than one
per task.

**If execution stalls or a task cannot complete**, honor `chain.md`'s recovery
(Resume / Skip / Abort). If the objective cannot be completed, do not close it
— jump to Step 6's "incomplete" branch and report what remains.

## Step 5: Roll Up the Objective

Read the aggregate picture from the objective brief read view:

```bash
tusk objective brief <OBJECTIVE_ID> --format markdown
```

This renders the status breakdown across linked tasks, criteria coverage,
summed cost/duration (counted per distinct task — no double-count), and any
open objective-scoped context. Show the markdown block to the user verbatim.

Then synthesize the per-task conclusions you observed in Step 4 into a short
summary:

- What the objective set out to do.
- Which linked tasks reached Done, and what each shipped (one line each).
- Any tasks that did not complete, with current status.
- Total cost/duration from the brief.

## Step 6: Decide Next Steps and Close the Objective

Decide from the Step 5 rollup:

- **All linked tasks Done and the goal is met** → close the objective as
  completed:
  ```bash
  tusk objective done <OBJECTIVE_ID> --reason completed
  ```

- **Execution showed the objective should not be pursued** (the work proved
  unnecessary, wrong, or out of scope) → close it as abandoned, and say why:
  ```bash
  tusk objective done <OBJECTIVE_ID> --reason abandoned
  ```
  `tusk objective done` closes the objective's own status only — it never
  changes the status of linked tasks, which remain the independent shippable
  unit.

- **Some linked tasks remain incomplete** → do **not** close the objective.
  Report exactly which tasks remain and their status, and tell the user how to
  resume (the objective and its links persist; re-run `chain.md <head_ids>` or
  `tusk.md <id>` for the stragglers, then re-run this prompt from Step 5 to
  roll up and close).

## Step 7: Final Report and Finish Cost Tracking

Print the final report:

- `OBJ-<id>` summary and final objective status (`completed` / `abandoned` /
  still `active`).
- Linked-task outcome table (id, summary, final status).
- The cost/duration totals from the Step 5 brief.
- Any newly unblocked backlog tasks `chain.md` surfaced.

Then close out the skill-run so its cost is captured:

```bash
tusk skill-run finish <run_id>
```

## Error Handling

- **`create-task.md` produced no new tasks** — it deduped the whole intent
  into existing tasks; offer to link those instead, else cancel the run and
  stop (Step 2).
- **Linking failed / objective has no linked tasks** — surface the error,
  cancel the run, stop (Step 3).
- **A task could not complete** — honor `chain.md`'s Resume/Skip/Abort
  recovery; leave the objective open and report remaining work (Steps 4 and 6).
- **VERSION/CHANGELOG conflicts** — never bump from this prompt; the single
  consolidated bump lives in `chain.md`'s Step 5. If a per-task bump caused a
  conflict, resolve it down to one bump for the whole objective.
