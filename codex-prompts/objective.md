# Objective — Plan or Run an Objective (Codex)

Separates objective planning from execution while preserving the original
end-to-end workflow. Planning delegates task-boundary reasoning to
`create-task.md` and persists one objective-aware atomic import. Execution
delegates to `chain.md`.

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

## Arguments and modes

- `/objective plan OBJ-N` — decompose an existing active objective, persist
  and display its task DAG, then stop before execution.
- `/objective run OBJ-N` — execute an already-planned active objective.
- `/objective full <freeform intent>` — create, plan, run, roll up, and close.
- `/objective <freeform intent>` — backward-compatible alias for `full`.

Parse the mode first. `plan` and `run` require `OBJ-N`; `full` requires an
initiative-level intent. With no argument, explain the modes and ask for an
intent to run in `full` mode.

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

This prints `{"id": N, "summary": "...", "status": "active", ...}`. Capture
`id` as `OBJECTIVE_ID` (the display form is `OBJ-<id>`). State it back to the
user verbatim: `Created OBJ-<id>: <summary>`.

> The objective summary/description go through the shared
> shell-metacharacter guard — do not embed backticks, `$(...)`, `${...}`, or
> bare `$IDENT` in either string; rewrite with plain words.

## Step 2: Plan Atomically via create-task.md

Run `create-task.md` with the objective intent and `OBJECTIVE_ID`. Follow it
through decomposition, deduplication, approval, criteria, dependency planning,
and objective-aware `task-import` materialization. It must use stable keys,
objective relationships on every item, `duplicate_policy: "skip"`, and one
default atomic import without `--best-effort`. Capture planned task IDs from
both `created.*.task_id` and `skipped.*.matched_task_id`. Never infer task
identity from task-number order or issue post-hoc per-task link writes.

## Step 3: Verify and Display the Plan

Confirm at least one task is linked:

```bash
tusk objective get <OBJECTIVE_ID>
```

The `tasks` array must be non-empty. Display a linked-task table and the
dependency-edge DAG from the confirmed import payload or `tusk deps list`.

For `plan`, report that the objective remains active, finish the objective
skill run, and stop. Do not invoke `chain.md`, roll up, or close the objective.

For `full`, continue immediately to Step 4.

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

- **The atomic plan resolved no task IDs** — cancel the run and leave the
  objective active for correction (Step 2).
- **Plan verification found no linked tasks** — surface the import result,
  cancel the run, and stop (Step 3).
- **A task could not complete** — honor `chain.md`'s Resume/Skip/Abort
  recovery; leave the objective open and report remaining work (Steps 4 and 6).
- **VERSION/CHANGELOG conflicts** — never bump from this prompt; the single
  consolidated bump lives in `chain.md`'s Step 5. If a per-task bump caused a
  conflict, resolve it down to one bump for the whole objective.
