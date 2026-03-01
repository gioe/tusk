---
name: groom-backlog
description: Groom the backlog by closing completed tickets, removing redundant/stale tickets, reprioritizing, and assigning agents
allowed-tools: Bash, Glob, Grep, Read
---

# Groom Backlog Skill

Grooms the local task database by identifying completed, redundant, incorrectly prioritized, or unassigned tasks.

> **Prefer `/create-task` for all task creation.** It handles decomposition, deduplication, acceptance criteria generation, and dependency proposals in one workflow. Use `bin/tusk task-insert` directly only when scripting bulk inserts or in automated contexts where the interactive review step is not applicable.

## Step 0: Start Cost Tracking

Record the start of this groom run so cost can be captured at the end:

```bash
tusk skill-run start groom-backlog
```

This prints `{"run_id": N, "started_at": "..."}`. Capture `run_id` — you will need it in Step 7.

## Setup: Fetch Config and Backlog

Before grooming, fetch everything needed in a single call:

```bash
tusk setup
```

This returns a JSON object with two keys:
- **`config`** — full project config (domains, agents, task_types, priorities, complexity, etc.). Use these values (not hardcoded ones) throughout the grooming process.
- **`backlog`** — all open tasks as an array of objects. Use this as the primary backlog data for Step 1 (you still need the dependency queries below).

## Pre-Check: Auto-Close Stale Tasks

Run all three auto-close checks (expired deferred, merged PRs, moot contingent) in a single command:

```bash
tusk autoclose
```

This returns a JSON summary with counts and task IDs per category:
- `expired_deferred` — deferred tasks past their 60-day expiry (closed as `expired`)
- `merged_prs` — In Progress tasks whose GitHub PR is already merged (closed as `completed`)
- `moot_contingent` — tasks contingent on upstream work that closed as `wont_do`/`expired` (closed as `wont_do`)
- `flagged_for_review` — (if present) In Progress tasks whose PR was closed without merging — flag these for user review in Step 3

If `total_closed` is 0, report "No auto-close candidates found" and proceed to Step 1. Otherwise, report the counts before continuing.

## Step 1: Fetch Dependency Data

The backlog tasks are already available from the `tusk setup` call above. Fetch dependency data to supplement:

```bash
tusk deps blocked
tusk deps all
```

**On-demand descriptions**: This query intentionally omits the `description` column to keep context lean. When you identify action candidates in Step 2 (tasks to close, delete, reprioritize, or assign), fetch full details for just those tasks:

```bash
tusk -header -column "SELECT id, summary, description FROM tasks WHERE id IN (<comma-separated ids>)"
```

## Step 1b: Consolidated Backlog Pre-flight Scan

Run the consolidated pre-flight scan to gather all grooming signals in a single call:

```bash
tusk backlog-scan
```

This returns JSON with four categories — hold onto the result, it replaces several individual queries throughout the workflow:
- **`duplicates`** — Heuristic duplicate pairs among open tasks (feeds Step 2a)
- **`unassigned`** — Open tasks with no assignee (feeds Category D)
- **`unsized`** — Open tasks without a complexity estimate (feeds Step 6)
- **`expired`** — Open tasks past their `expires_at` date; if non-empty, report those task IDs alongside the `tusk autoclose` results from the Pre-Check step as additional candidates for manual review

## Step 2: Scan for Duplicates and Categorize Tasks

### Step 2a: Scan for Duplicate Pairs

Use the **`duplicates`** array from the `tusk backlog-scan` result (Step 1b). Each entry is:
```json
{"task_a": {"id": N, "summary": "..."}, "task_b": {"id": N, "summary": "..."}, "similarity": 0.N}
```

Any pairs found should be included in **Category B** with reason "duplicate".

### Step 2b: Categorize Tasks

Analyze each task and categorize. In addition to the heuristic scan results from Step 2a, look for **semantic duplicates** — tasks that cover the same intent but use different wording (e.g., "Implement password reset flow" vs. "Add forgot password endpoint"). The heuristic catches textual near-matches; you should catch conceptual overlap that differs in phrasing.

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
Tasks without an agent assignee. Use the **`unassigned`** array from the `tusk backlog-scan` result (Step 1b) — each entry includes `id`, `summary`, and `domain`.

Assign based on project agents (from `tusk config`).

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

## Step 4: Get User Confirmation

**IMPORTANT**: Before making any changes, explicitly ask the user to approve each category.

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

Before computing priority scores, check for tasks without complexity estimates. Use the **`unsized`** array from the `tusk backlog-scan` result (Step 1b) — each entry includes `id`, `summary`, `domain`, and `task_type`.

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
- **Moved to Done**: W tasks
- **Deleted**: X tasks
- **Reprioritized**: Y tasks
- **Assigned**: U tasks
- **Unchanged**: Z tasks
```

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

**Keep the backlog lean (< 20 open tasks)**: The full backlog dump scales at ~700 tokens/task and is repeated across ~15+ agentic turns during grooming. A 30-task backlog can consume over 300k tokens in a single session. Aggressively close, merge, or defer tasks to stay under 20 open items.
