# Create Task — Decompose Freeform Text into Tusk Tasks (Codex)

Takes arbitrary text input — feature specs, meeting notes, brainstorm
lists, bug reports, requirements docs — and decomposes it into structured,
deduplicated tasks in the tusk database. Pure CLI flow: every action is a
`tusk` subcommand.

> **Conventions:** Run `tusk conventions search <topic>` for project rules
> (commits, structure, testing, migrations, skill authoring, criteria
> shape). Do not restate convention text inline — it drifts from the DB.

## Step 1: Capture Input

The user supplies freeform text. If they invoked the prompt with no text,
ask:

> What would you like to turn into tasks? Paste any text — feature specs,
> meeting notes, bug reports, requirements, etc.

## Step 2: Fetch Config and Backlog

```bash
tusk setup
```

Returns:

- `config` — full project config (`domains`, `task_types`, `agents`,
  `priorities`, `complexity`, etc.). When a list is empty (e.g.
  `"domains": []`), that field has no validation; use your judgment or
  leave it NULL. Pass only the values that match the configured set —
  `tusk task-insert` rejects values outside it.
- `backlog` — every open task as JSON. Hold this in working context for
  Step 3's semantic-duplicate check (the heuristic
  `tusk dupes check` runs inside `tusk task-insert`, but it catches
  textual similarity only — semantic duplicates are your job).

## Step 2.5: Bundled-Scope Pre-Check

Before drafting tasks, scan the input for bundled-scope markers
(issue #782). A bundle is suspected if any of these patterns appears
in the summary or description:

- ` + ` between two named features in the summary (e.g.
  `Add A + B`)
- `: ` followed by a connector list in the summary (e.g.
  `flourishes: X, Y, and Z`)
- Numbered enumeration `(1)` / `(2)` / `1.` / `2.` introducing
  distinct deliverables in the description
- Quantity-connector phrases in the description:
  `both X and Y`, `X as well as Y`, `two <nouns>`,
  `three <nouns>`, `each of`

Do NOT fire on incidental connectives where one side is naturally
subordinate to the other:

- `add X and update Y's docs` — Y's docs naturally completes X
- `fix bug X and add regression test` — the test verifies the fix
- `refactor module and rename file` — the rename is incidental

When any marker fires, surface an informational advisory naming the
matched pattern verbatim — this is a heads-up, not a gate. The
commit-time scope guard is the real enforcement boundary; bundled
tasks fail loudly at commit time when the agent tries to commit the
second deliverable outside the originally-named files, so this
prompt is a UX courtesy that lets the operator decide before the
draft instead of after the rejected commit.

> Heads up — input appears to bundle multiple deliverables
> (matched: `<verbatim quote>`). The scope guard will likely reject
> mid-task commits that wander outside the originally-named files.
> Options: Show me the proposal first (default) / Split / Keep as
> one.

On Split, decompose into N sibling tasks in Step 3. On Show me
first or Keep as one, continue to Step 3 unchanged; the operator
can revisit after Step 4's review.

## Step 3: Analyze and Decompose

For each candidate task, fill these fields:

| Field | How to determine |
|-------|-----------------|
| **summary** | Imperative sentence describing the deliverable (max ~100 chars). |
| **description** | Concise motivation and constraints from the input. Keep acceptance hints in criteria, technical pickup facts in context atoms, and implementation plans out of the description. |
| **priority** | Infer from language: "critical"/"urgent"/"blocking" → `Highest`/`High`; "nice to have"/"eventually" → `Low`/`Lowest`; default `Medium`. Must match a configured priority. |
| **domain** | Map to a configured domain by subject area. Leave NULL if none fit or domains aren't configured. |
| **task_type** | One of the configured types (typically bug, feature, refactor, test, docs, infrastructure). Default `feature` for new work, `bug` for fixes. See decision guide below. |
| **assignee** | Match to a configured agent if the work clearly belongs there. NULL if unsure. |
| **complexity** | `XS` partial session · `S` 1 session · `M` 2–3 sessions · `L` 3–5 sessions · `XL` 5+. Default `M`. |

### Task Type Decision Guide

The key question: **Is this type the primary deliverable, or is it proof
that another deliverable is done?**

| Type | Use as `task_type` when… | Use as acceptance criterion when… |
|------|---------------------------|------------------------------------|
| **bug** | The deliverable is fixing a defect. | A regression must not recur. |
| **feature** | The deliverable is new functionality. | (Always a task — never a criterion.) |
| **refactor** | The deliverable is restructuring without behavior change. | (Always a task.) |
| **test** | Writing tests **is the goal**. | Tests verify a feature is done. |
| **docs** | Writing docs **is the goal**. | Docs confirm completion. |
| **infrastructure** | The deliverable is tooling/CI/infra. | (Always a task.) |

**Rule:** If removing the work would leave the *feature itself* incomplete
→ task_type. If removing it just removes *verification* of an already-
complete feature → criterion.

### Decomposition Guidelines

- One task per deliverable; split features with multiple distinct pieces.
- Each task should be completable in a single focused session.
- Preserve only shippable context in the description; route durable handoff
  facts to context atoms.
- Don't over-split. Trivial sub-steps that are part of a larger task stay
  in the same row. A good test: if a sub-task **cannot be tested or
  delivered independently** of the parent, it belongs in the same ticket.
- Group closely related fixes if they share a root cause.
- Cross-check each proposal against the `backlog` from Step 2 — if an
  existing task covers the same intent with different wording, flag it as
  a duplicate rather than proposing a new task.

### Durable Context Atoms

Some source material should survive handoff but is not a requirement or a
completion condition. Track these as candidate context atoms while drafting,
then write them after `tusk task-insert` returns the created task ID.

Use the smallest unit:

| Information | Destination |
|-------------|-------------|
| Requirement that must ship | Task or criterion |
| Condition proving completion | Criterion |
| Assumption future agents must preserve | `tusk context add --type assumption` |
| Risk or trigger condition | `tusk context add --type risk` |
| Non-blocking open ambiguity | `tusk context add --type question` |
| Chosen design/product direction | `tusk context add --type decision` |
| Reusable handoff fact | `tusk context add --type memory` |
| Stable pickup starting point | `tusk context add --type entry_point` |

Do not write directly to `task_context_items`. Use:

```bash
tusk context add <task_id> --source create_task --type memory --content "<content>"
```

## Step 3.5: Pre-Verify Bug Test Failures

**Run this step only when the input describes a bug that claims a specific
test is failing.** Skip otherwise.

Trigger signals (any one is enough):

- Phrases like "pre-existing failing test", "test is failing", "failing
  test", or naming a specific test file/function alongside the bug.
- Step 3's `task_type` is `bug` AND the description references a test by
  name or path.

If triggered:

1. Detect the test command:
   ```bash
   tusk test-detect
   ```
   If `confidence` is `"none"`, skip the rest of this step.
2. Run the referenced test directly. Cap at 60 seconds. If the run errors
   for unrelated reasons (import error, missing dependency), skip the rest
   of this step.
3. Evaluate:
   - **Test fails** — failure confirmed; proceed to Step 4 silently.
   - **Test passes** — surface before presenting:
     > **Pre-verification note:** the referenced test is currently
     > **passing** on this branch. Still create a bug task?
     Wait for the user. Stop on no/cancel; continue on confirm.

## Step 3.6: Detect Fix / Follow-up Linkage

Scan each proposed task's summary and description (case-insensitive) for:

- `fixes TASK-N`
- `follow-up from TASK-N` / `follow up from TASK-N`
- `retro follow-up from TASK-N`

When matched:

1. Parse `N`.
2. Verify `N` exists — check the backlog from Step 2 first; otherwise:
   ```bash
   tusk task-get N
   ```
   If `N` doesn't resolve, drop the linkage silently (informal phrasing).
3. Reject self-reference if obvious from the summary.
4. Record `fixes_task_id = N` for use by Step 5.

If a single task's text mentions **two or more** different `TASK-N`
identifiers via the above phrasing, ask the user to disambiguate before
Step 4. Mere mentions like "see TASK-N" or "related to TASK-N" do **not**
qualify — leave `fixes_task_id` unset.

## Step 4: Present Task List for Review

### Single-task fast path

If exactly one task was produced, use the inline format:

```markdown
## Proposed Task

**Add login endpoint with JWT auth** (High · api · feature · M · backend)
> Implement POST /auth/login that validates credentials and returns a JWT
> token. Include refresh token support.
```

Ask: **Confirm**, **edit** (e.g. "change priority to Medium"), or
**remove**.

### Multi-task presentation

If two or more tasks were produced, show a numbered table:

```markdown
## Proposed Tasks

| # | Summary | Priority | Domain | Type | Complexity | Assignee |
|---|---------|----------|--------|------|------------|----------|
| 1 | Add login endpoint with JWT auth | High | api | feature | M | backend |
| 2 | Add signup page with form validation | Medium | frontend | feature | S | frontend |

### Details

**1. Add login endpoint with JWT auth**
> Implement POST /auth/login that validates credentials and returns a JWT
> token. Include refresh token support.
```

Ask: **Confirm**, **remove N**, **edit N field=value**, or **add a
missing task**.

### Both paths

Wait for explicit user approval before inserting. Never insert without
confirmation.

If any task has candidate context atoms, show them under **Durable context**
in the proposal and let the operator confirm, edit, or remove them. These
atoms are written with `tusk context add --source create_task` after the task
row exists, not embedded in the description.

## Step 5: Generate Criteria, Deduplicate, Insert

For each approved task, generate **3–7 acceptance criteria** — concrete,
testable conditions that define "done." Derive them from the description:

- Each distinct requirement maps to a criterion.
- For **bug** tasks, include a criterion that the failure case is resolved.
- For **feature** tasks, include the happy path and at least one edge case.
- For any task that creates a new DB table (or sits in a schema-related
  domain), always include: "DOMAIN.md updated with schema entry for
  `<table_name>`".

### Dangerous Criterion Guard

Never generate a criterion whose text contains:

- `tusk init --force` — wipes the live task DB
- `git reset --hard` — discards uncommitted work
- `git push --force` / `git push -f` — overwrites remote history
- `rm -rf` — recursive deletion
- `DROP TABLE` / `DROP DATABASE` — destructive SQL

**Init verification redirect:** if a task verifies `tusk init` behavior,
target the integration suite instead of the live DB:

> `python3 -m pytest tests/integration/ -k test_init -q` passes

If any generated criterion matches a prohibited pattern, stop, warn the
user, and revise before inserting:

> ⚠️ **Dangerous criterion detected**: `"<text>"` contains `<pattern>`.
> Replace with a safer alternative (e.g., an integration test assertion).

### Insert

```bash
tusk task-insert "<summary>" "<description>" \
  --priority "<priority>" \
  --domain "<domain>" \
  --task-type "<task_type>" \
  --assignee "<assignee>" \
  --complexity "<complexity>" \
  --criteria "<criterion 1>" \
  --criteria "<criterion 2>" \
  --criteria "<criterion 3>"
```

Append `--fixes-task-id <N>` if Step 3.6 found a linkage.

For typed criteria with automated verification, use `--typed-criteria`
with a JSON object:

```bash
tusk task-insert "<summary>" "<description>" \
  --criteria "Manual criterion" \
  --typed-criteria '{"text":"Tests pass","type":"test","spec":"pytest tests/"}' \
  --typed-criteria '{"text":"Config exists","type":"file","spec":"config/*.json"}'
```

Valid types: `manual` (default), `code`, `test`, `file`. Non-manual types
require a `spec`. Omit `--domain` or `--assignee` entirely when the value
is NULL — do not pass empty strings.

For pytest `test` specs, include the full node ID. If the test is defined
inside a class, include the class segment:
`tests/path/test_file.py::TestClassName::test_method_name`, not
`tests/path/test_file.py::test_method_name`. The shorter form points pytest
at a module-level function and fails with "not found" even when the
class-contained test exists.

### Exit codes

- **0** — success. Output JSON includes `task_id` and `criteria_ids`.
  Capture `task_id` for Step 7's dependency proposals. Then write any
  operator-approved durable context atoms for that task:
  ```bash
  tusk context add <task_id> --source create_task --type risk --content "<content>"
  ```
  Use the confirmed type for each atom (`memory`, `assumption`, `question`,
  `risk`, `decision`, or `entry_point`) and capture the returned context item
  IDs for the final summary.
- **1** — duplicate found. Output JSON includes `matched_task_id` and
  `similarity`. Report which existing task matched and skip:
  > Skipped "<summary>" — duplicate of existing task #N (similarity 0.87)
- **2** — error. Surface the message and skip.

## Step 6: (Reserved)

(Step numbering follows the original `/create-task` flow; the original
Step 6 was merged into Step 5 here.)

## Step 7: Propose Dependencies

Skip this step when:

- Zero tasks were created (all duplicates), OR
- Exactly one task was created.

For two or more created tasks, scan for ordering relationships:

- **blocks** — task A's deliverable must exist before task B can start
  (hard prerequisite).
- **contingent** — task B is *worth doing only if* task A's outcome
  warrants it (soft, often used for evaluations leading to follow-up
  work).

Present proposals to the user for confirmation. Then add each:

```bash
tusk deps add <task_id> <depends_on_id> [--type blocks|contingent]
```

Don't propose more than necessary — most independent tasks need no edges.

## Step 8: Report Results

```markdown
## Results

**Created**: 3 tasks (#14, #15, #16)
**Skipped**: 1 duplicate (matched existing #12)
**Dependencies added**: 2 (#16 → #14 (blocks), #17 → #14 (contingent))

| ID | Summary | Priority | Domain |
|----|---------|----------|--------|
| 14 | Add signup page with form validation | Medium | frontend |
| 15 | Fix broken CSS on mobile nav | High | frontend |
| 16 | Add rate limiting middleware | Medium | api |
```

Show the **Dependencies added** line only when Step 7 inserted edges —
omit when skipped or when the user removed all proposals.

### Zero-criteria check

For each created task ID, verify at least one criterion exists:

```bash
tusk criteria list <task_id>
```

If any task has zero criteria, warn and stop:

> **Warning**: Tasks #14, #16 have no acceptance criteria. Generate
> criteria for them before continuing.

### Show updated backlog

- More than 3 created — print the full open backlog so the user can see
  where the new tasks landed:
  ```bash
  tusk -header -column "SELECT id, summary, priority, domain, task_type, assignee FROM tasks WHERE status = 'To Do' ORDER BY priority_score DESC, id"
  ```
- 3 or fewer — print only a count to save tokens:
  ```bash
  tusk "SELECT COUNT(*) || ' open tasks in backlog' FROM tasks WHERE status = 'To Do'"
  ```
