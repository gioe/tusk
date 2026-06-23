---
name: groom-backlog
description: Groom the backlog by closing completed tickets, removing redundant/stale tickets, reprioritizing, and assigning agents
allowed-tools: Bash, Glob, Grep, Read
model: sonnet
---

# Groom Backlog Skill

Grooms the local task database by identifying completed, redundant, incorrectly prioritized, or unassigned tasks.

> Use `/create-task` for task creation — handles decomposition, deduplication, criteria, and deps. Use `tusk task-insert` only for bulk/automated inserts.

## Step 0: Start Cost Tracking

Record the start of this groom run so cost can be captured at the end:

```bash
tusk skill-run start groom-backlog
```

This prints `{"run_id": N, "started_at": "..."}`. Capture `run_id` — you will need it in Step 7.

> **Early-exit cleanup:** If any check below causes the skill to stop before Step 7b (e.g., `tusk groom` fails, the backlog is empty with nothing to groom, or the user declines the Step 4 approval prompt), first call `tusk skill-run cancel <run_id>` to close the open row, then stop. Otherwise the row lingers as `(open)` in `tusk skill-run list` forever.

## Setup: Run the Mechanical Pipeline

```bash
tusk groom
```

`tusk groom` is a single CLI orchestrator that runs autoclose, scope rederive (open tasks), backlog-scan (duplicates / unassigned / unsized / expired), and lint in sequence. It returns one JSON document with these keys:

- `expired` — open tasks past their `expires_at` date
- `duplicates` — heuristic duplicate pairs among open tasks
- `unassigned` — To Do tasks with no assignee
- `unsized` — To Do tasks with no complexity estimate
- `autoclose_candidates` — `{moot_contingent, total, applied}`
- `scope_rederive` — `{applied, tasks_processed, tasks_changed, results}`
- `lint` — `{exit_code, summary}` from `tusk lint --quiet`

Hold this JSON in memory for the rest of the flow. **Do not** run `tusk autoclose`, `tusk scope rederive --all`, `tusk backlog-scan`, or `tusk lint` separately — the orchestrator already invoked them.

If `autoclose_candidates.applied > 0`, report the counts before continuing. If every list is empty and `lint.exit_code` is 0, the backlog is healthy — skip to Step 7 and report.

> **Dry-run preview.** To inspect what `tusk groom` *would* close without applying changes, run:
> ```bash
> tusk groom --dry-run
> ```
> The `autoclose_candidates.applied` flag is `false` and the same candidate IDs appear under `autoclose_candidates.moot_contingent` for review. The `scope_rederive.applied` flag is likewise `false` (the rederive mutation is skipped, with `tasks_processed` and `tasks_changed` both 0). The backlog-scan and lint steps run unchanged either way.

## Pre-Check: Rederive Stale Scope Rows

`tusk groom` already ran this pass for you — the `scope_rederive` key in the Setup JSON is its rollup. The orchestrator runs the bulk rederive over **open tasks only**, rebuilding stale `auto_derived` scope rows so the spurious `missing_scope_path` context-health warnings they produce are cleaned up automatically during routine grooming. **Do not** run `tusk scope rederive --all` separately. The `scope_rederive` rollup shape:

```json
{"applied": true, "tasks_processed": N, "tasks_changed": M,
 "results": [{"task_id": N, "removed": ["..."], "added": ["..."], "auto_derived": ["..."], "preserved": [...]}, ...]}
```

Report `tasks_changed` (`M`) and, for each entry in `results` whose `removed` or `added` array is non-empty, a one-line `TASK-<task_id>: -<removed> +<added>` rollup. If `tasks_changed` is 0, report "No stale scope rows found" and proceed to Step 1. In a `tusk groom --dry-run` preview, `scope_rederive.applied` is `false` and `results` is empty (the mutation was skipped).

**Why this runs automatically (not behind the Step 4 approval gate):** like the autoclose pass inside `tusk groom`, this is a safe pre-approval mutation. It is in fact *strictly safer* than autoclose — autoclose changes task **status** (closes tasks), whereas rederive only rebuilds derived metadata: it deletes and rebuilds `auto_derived` scope rows from the task's current text while leaving every `operator_declared`, `creates`, and `unbounded` row untouched. No operator intent is lost and no task status changes, so there is nothing for the user to approve. This is the auto-cleanup pass that keeps grooming a backlog-hygiene operation rather than a read-only audit.

## Step 1: Fetch Config, Backlog, and Dependency Data

The mechanical signals are now in hand. Fetch context for the analysis layer:

```bash
tusk setup
tusk deps blocked
tusk deps all
```

`tusk setup` returns `{config, backlog}`. Use `config` for valid domain / agent / priority values (not hardcoded ones) throughout the grooming process, and `backlog` as the authoritative open-task list for Step 2's semantic-duplicate sweep.

**On-demand descriptions**: This query intentionally omits the `description` column to keep context lean. When you identify action candidates in Step 2 (tasks to close, delete, reprioritize, or assign), fetch full details for just those tasks:

```bash
tusk -header -column "SELECT id, summary, description FROM tasks WHERE id IN (<comma-separated ids>)"
```

## Step 2: Categorize Tasks

The `duplicates`, `unassigned`, `unsized`, and `expired` arrays from `tusk groom` (Setup) drive most categories:
- **`duplicates`** — heuristic duplicate pairs among open tasks. Each entry is `{"task_a": {"id": N, "summary": "..."}, "task_b": {"id": N, "summary": "..."}, "similarity": 0.N}`; include any pairs found in **Category B** with reason "duplicate".
- **`unassigned`** — open tasks with no assignee (feeds Category D).
- **`unsized`** — open tasks without a complexity estimate (feeds Step 6).
- **`expired`** — open tasks past their `expires_at` date; if non-empty, report those task IDs alongside the `autoclose_candidates` from the Setup pipeline as additional candidates for manual review.

In addition to the heuristic scan results, look for **semantic duplicates** — tasks that cover the same intent but use different wording (e.g., "Implement password reset flow" vs. "Add forgot password endpoint"). The heuristic catches textual near-matches; you should catch conceptual overlap that differs in phrasing.

### Category A: Candidates for Done (Acceptance Criteria Already Met)
Tasks where the work has already been completed in the codebase:
1. **Verify against code**: Search the codebase to determine if the work is done
2. **Evidence required**: Provide specific file paths and code as proof
3. **Mark as Done**:
   ```bash
   tusk task-done <id> --reason completed
   ```

### Category B: Candidates for Deletion
- **Redundant tasks**: Duplicates or near-duplicates
- **Obsolete tasks**: No longer relevant
- **Stale tasks**: Untouched with no clear path forward
- **Vague tasks**: Insufficient detail to act on

Before recommending deletion, check dependents:
```bash
tusk deps dependents <id>
```

### Category C: Candidates for Reprioritization
- **Under-prioritized**: Security issues, user-facing bugs
- **Over-prioritized**: Nice-to-haves, speculative work

### Category D: Unassigned Tasks
Tasks without an agent assignee. Use the **`unassigned`** array from the `tusk groom` result (Setup) — each entry includes `id`, `summary`, and `domain`.

Assign based on project agents (from the `config` returned by `tusk setup` in Step 1).

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

If `lint.exit_code` from the Setup pipeline is non-zero, append a one-line note: the lint summary is informational here — it doesn't block grooming, but the user may want to address violations before the next session.

## Step 4: Get User Confirmation

**IMPORTANT**: Before making any changes, explicitly ask the user to approve each category.

If the user declines approval (or no actions were proposed), run `tusk skill-run cancel <run_id>` and stop — do not proceed to Step 5. This closes the open `skill_runs` row instead of leaving it pending forever.

## Step 5: Execute Changes

Only after user approval:

### For Done Transitions:
```bash
tusk task-done <id> --reason completed
```

### For Deletions:
```bash
# Duplicates:
tusk task-done <id> --reason duplicate

# Obsolete/won't-do:
tusk task-done <id> --reason wont_do
```

### For Priority Changes:
```bash
tusk task-update <id> --priority "<New Priority>"
```

**Auto-prioritize skill-patch follow-up tasks.** Skill-patch follow-up tasks (created by `/retro` from deferred skill/doc-patch findings) frequently land at the unmodified default priority and rot in the backlog. For any open task that is a skill/doc-patch follow-up, derive its priority from its retro-signals — reopen counts, rework chains (fixes/fixed_by), and recurring review themes — and apply it. Higher reopen and rework counts yield a higher priority:
```bash
tusk skill-patch-priority <id> --apply
```
The command is a no-op when the task carries no rework history (it stays at default) and lifts the priority otherwise, so it is safe to run across every candidate skill-patch task. Count each task it changes toward the `tasks_reprioritized` metric in Step 7's `skill-run finish` rollup.

### For Agent Assignments:
```bash
tusk task-update <id> --assignee "<agent-name>"
```

### After All Changes:

Verify all modifications in a single batch query:

```bash
tusk -header -column "SELECT id, summary, status, priority, assignee FROM tasks WHERE id IN (<comma-separated ids of all changed tasks>)"
```

## Step 6: Bulk-Estimate Unsized Tasks

Before computing priority scores, check for tasks without complexity estimates. Use the **`unsized`** array from the `tusk groom` result (Setup) — each entry includes `id`, `summary`, `domain`, and `task_type`.

If the `unsized` array is empty, skip to Step 7.

If unsized tasks are found, read the reference file for the sizing workflow:

```
Read file: <base_directory>/REFERENCE.md
```

Follow Steps 6b–6d from the reference, then continue to Step 7 below.

## Step 7: Final Report

Generate the summary report:

```markdown
## Backlog Grooming Complete

### Actions Taken:
- **Auto-closed** (from `tusk groom`): T tasks
- **Scope rows rederived** (from the Pre-Check rederive pass): M tasks changed
- **Moved to Done**: W tasks
- **Deleted**: X tasks
- **Reprioritized**: Y tasks
- **Assigned**: U tasks
- **Sized**: S tasks
- **Unchanged**: Z tasks
```

Use the `autoclose_candidates.applied`/`moot_contingent` counts (`T`) and the `scope_rederive.tasks_changed` count (`M`) from the `tusk groom` output captured in Setup.

Show the final backlog state (this also serves as WSJF score verification):

```bash
tusk -header -column "
SELECT id, summary, status, priority, complexity, priority_score, domain, assignee
FROM tasks
WHERE status <> 'Done'
ORDER BY priority_score DESC, id
"
```

## Step 7b: Finish Cost Tracking

Record cost for this groom run. Replace `<run_id>` with the value captured in Step 0, and fill in the actual counts from the actions taken:

```bash
tusk skill-run finish <run_id> --metadata '{"tasks_done":<W>,"tasks_deleted":<X>,"tasks_reprioritized":<Y>,"tasks_assigned":<U>}'
```

This reads the Claude Code transcript for the time window of this run and stores token counts and estimated cost in the `skill_runs` table.

To view cost history across all groom runs:

```bash
tusk skill-run list groom-backlog
```

## Headless / CI Usage

`/groom-backlog` can be run unattended via `claude -p` (non-interactive print mode):

```bash
claude -p /groom-backlog
```

**Caveats for unattended runs:**
- **Step 4 user confirmation is typically skipped by the LLM in non-interactive mode**, so all recommendations from Steps 2–3 are likely applied automatically without approval. This is LLM behavior, not a hard-coded code path — there is no guarantee. Use this only on a trusted backlog where auto-apply is acceptable.
- Best suited for scheduled maintenance jobs (e.g., nightly cron or CI pipelines) where the goal is to keep the backlog clean without manual intervention.
- Review the run output afterward to audit what was changed.

## Important Guideline

**Keep the backlog lean (< 20 open tasks)**: The full backlog dump scales at ~700 tokens/task and is repeated across ~15+ agentic turns during grooming. A 30-task backlog can consume over 300k tokens in a single session. Aggressively close or merge tasks to stay under 20 open items.
