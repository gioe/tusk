# Groom Backlog — Auto-Close, Dedup, Reprioritize, Reassign (Codex)

Grooms the local task database by identifying completed, redundant,
incorrectly prioritized, or unassigned tasks. The mechanical pipeline
(autoclose + backlog-scan + lint) lives behind a single CLI orchestrator;
this prompt drives it and adds the interactive analysis layer.

> **Conventions:** Run `tusk conventions search <topic>` for project rules.
> Do not restate convention text inline — it drifts from the DB.

> Use `create-task.md` for task creation — handles decomposition,
> deduplication, criteria, and deps. Use `tusk task-insert` only for
> bulk/automated inserts.

## Step 0: Start Cost Tracking

```bash
tusk skill-run start groom-backlog
```

This prints `{"run_id": N, "started_at": "..."}`. Capture `run_id` —
needed in Step 7b.

> **Early-exit cleanup:** If any check below causes the prompt to stop
> before Step 7b (e.g., `tusk groom` fails, the backlog is empty with
> nothing to groom, or the user declines the Step 4 approval prompt),
> first call `tusk skill-run cancel <run_id>` to close the open row, then
> stop. Otherwise the row lingers as `(open)` in `tusk skill-run list`
> forever.

## Setup: Run the Mechanical Pipeline

```bash
tusk groom
```

`tusk groom` is a single CLI orchestrator that runs autoclose,
backlog-scan (duplicates / unassigned / unsized / expired), and lint in
sequence. It returns one JSON document with these keys:

- `expired` — open tasks past their `expires_at` date
- `duplicates` — heuristic duplicate pairs among open tasks
- `unassigned` — To Do tasks with no assignee
- `unsized` — To Do tasks with no complexity estimate
- `autoclose_candidates` — `{moot_contingent, total, applied}`
- `lint` — `{exit_code, summary}` from `tusk lint --quiet`

Hold this JSON in memory for the rest of the flow. **Do not** run
`tusk autoclose`, `tusk backlog-scan`, or `tusk lint` separately — the
orchestrator already invoked them.

If `autoclose_candidates.applied > 0`, report the counts before
continuing. If every list is empty and `lint.exit_code` is 0, the backlog
is healthy — skip to Step 7 and report.

> **Dry-run preview.** To inspect what `tusk groom` *would* close
> without applying changes, run:
> ```bash
> tusk groom --dry-run
> ```
> The `autoclose_candidates.applied` flag is `false` and the same
> candidate IDs appear under `autoclose_candidates.moot_contingent`
> for review. The backlog-scan and lint steps run unchanged either
> way.

## Step 1: Fetch Config, Backlog, and Dependency Data

The mechanical signals are now in hand. Fetch context for the analysis
layer:

```bash
tusk setup
tusk deps blocked
tusk deps all
```

`tusk setup` returns `{config, backlog}`. Use `config` for valid domain /
agent / priority values during reassignment, and `backlog` as the
authoritative open-task list for Step 2's semantic-duplicate sweep.

**On-demand descriptions:** `tusk setup`'s backlog intentionally omits
some long-text columns to keep context lean. When you identify action
candidates in Step 2 (tasks to close, delete, reprioritize, or assign),
fetch full details for just those tasks:

```bash
tusk -header -column "SELECT id, summary, description FROM tasks WHERE id IN (<comma-separated ids>)"
```

## Step 2: Categorize Tasks

The `duplicates`, `unassigned`, `unsized`, and `expired` arrays from
`tusk groom` (Step 0) drive most categories. In addition to the
heuristic scan results, look for **semantic duplicates** — tasks that
cover the same intent but use different wording (e.g., "Implement
password reset flow" vs. "Add forgot password endpoint"). The heuristic
catches textual near-matches; you should catch conceptual overlap that
differs in phrasing.

### Category A: Candidates for Done (Acceptance Criteria Already Met)

Tasks where the work has already been completed in the codebase:
1. **Verify against code:** Search the codebase to determine if the work
   is done.
2. **Evidence required:** Provide specific file paths and code as proof.
3. **Mark as Done:**
   ```bash
   tusk task-done <id> --reason completed
   ```

### Category B: Candidates for Deletion

- **Redundant tasks:** Duplicates or near-duplicates (use the
  `duplicates` array from Step 0).
- **Obsolete tasks:** No longer relevant.
- **Stale tasks:** Untouched with no clear path forward.
- **Vague tasks:** Insufficient detail to act on.

Before recommending deletion, check dependents:
```bash
tusk deps dependents <id>
```

### Category C: Candidates for Reprioritization

- **Under-prioritized:** Security issues, user-facing bugs.
- **Over-prioritized:** Nice-to-haves, speculative work.

### Category D: Unassigned Tasks

Tasks without an agent assignee. Use the `unassigned` array from
`tusk groom` — each entry includes `id`, `summary`, and `domain`.

Assign based on project agents (from the `config` returned by
`tusk setup` in Step 1).

### Category E: Healthy Tasks

Correctly prioritized, assigned, and relevant. No action needed.

## Step 3: Present Findings for Approval

Present analysis in this format:

```markdown
## Backlog Grooming Analysis

### Total Tasks Analyzed: X

### Ready for Done (W tasks)
| ID | Summary | Evidence |

### Recommended for Deletion (Y tasks)
| ID | Summary | Reason |

### Recommended for Reprioritization (Z tasks)
| ID | Summary | Current | Recommended | Reason |

### Unassigned Tasks (U tasks)
| ID | Summary | Recommended Agent | Reason |

### No Action Needed (V tasks)
```

If `lint.exit_code` from Step 0 is non-zero, append a one-line note: the
lint summary is informational here — it doesn't block grooming, but the
user may want to address violations before the next session.

## Step 4: Get User Confirmation

**IMPORTANT:** Before making any changes, explicitly ask the user to
approve each category.

If the user declines approval (or no actions were proposed), run
`tusk skill-run cancel <run_id>` and stop — do not proceed to Step 5.
This closes the open `skill_runs` row instead of leaving it pending
forever.

## Step 5: Execute Changes

Only after user approval:

### For Done Transitions

```bash
tusk task-done <id> --reason completed
```

### For Deletions

```bash
# Duplicates:
tusk task-done <id> --reason duplicate

# Obsolete/won't-do:
tusk task-done <id> --reason wont_do
```

### For Priority Changes

```bash
tusk task-update <id> --priority "<New Priority>"
```

### For Agent Assignments

```bash
tusk task-update <id> --assignee "<agent-name>"
```

### After All Changes

Verify all modifications in a single batch query:

```bash
tusk -header -column "SELECT id, summary, status, priority, assignee FROM tasks WHERE id IN (<comma-separated ids of all changed tasks>)"
```

## Step 6: Bulk-Estimate Unsized Tasks

Use the `unsized` array from `tusk groom` (Step 0) — each entry includes
`id`, `summary`, `domain`, and `task_type`.

If the `unsized` array is empty, skip to Step 7.

For each unsized task:

1. Read its full description (`tusk task-get <id>`).
2. Estimate complexity from scope:
   - **XS** — partial session
   - **S** — 1 session
   - **M** — 2–3 sessions
   - **L** — 3–5 sessions
   - **XL** — 5+ sessions
3. Apply with:
   ```bash
   tusk task-update <id> --complexity <XS|S|M|L|XL>
   ```

Bulk-prompt the user to approve all proposed sizes in a single message
before running the updates. After applying, the `priority_score` view
recomputes automatically.

## Step 7: Final Report

Generate the summary report:

```markdown
## Backlog Grooming Complete

### Actions Taken:
- **Auto-closed** (from `tusk groom`): T tasks
- **Moved to Done**: W tasks
- **Deleted**: X tasks
- **Reprioritized**: Y tasks
- **Assigned**: U tasks
- **Sized**: S tasks
- **Unchanged**: Z tasks
```

Show the final backlog state (this also serves as WSJF score
verification):

```bash
tusk -header -column "
SELECT id, summary, status, priority, complexity, priority_score, domain, assignee
FROM tasks
WHERE status <> 'Done'
ORDER BY priority_score DESC, id
"
```

## Step 7b: Finish Cost Tracking

Record cost for this groom run. Replace `<run_id>` with the value
captured in Step 0, and fill in the actual counts:

```bash
tusk skill-run finish <run_id> --metadata '{"tasks_done":<W>,"tasks_deleted":<X>,"tasks_reprioritized":<Y>,"tasks_assigned":<U>}'
```

To view cost history across all groom runs:

```bash
tusk skill-run list groom-backlog
```

## Headless / CI Usage

`/groom-backlog` can be run unattended via Codex's non-interactive print
mode.

**Caveats for unattended runs:**
- **Step 4 user confirmation may be skipped by the runner in
  non-interactive mode**, so all recommendations from Steps 2–3 are
  likely applied automatically without approval. Use this only on a
  trusted backlog where auto-apply is acceptable.
- Best suited for scheduled maintenance jobs (e.g., nightly cron) where
  the goal is to keep the backlog clean without manual intervention.
- Review the run output afterward to audit what was changed.

## Important Guideline

**Keep the backlog lean (< 20 open tasks):** The full backlog dump
scales at ~700 tokens/task and is repeated across many turns during
grooming. A 30-task backlog can consume over 300k tokens in a single
session. Aggressively close or merge tasks to stay under 20 open items.
