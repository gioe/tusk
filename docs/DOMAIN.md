# Tusk Domain Model

This document codifies the entity/attribute model, allowed status transitions, invariants, and relationship semantics for the tusk task management system. It is the authoritative reference for schema migrations, skill authoring, and AI context.

---

## Database Scope and Portability

**The tusk database is single-node and local-only.** It is not designed to sync across machines.

- **SQLite is inherently single-node.** The database file (`tusk/tasks.db`) lives on your local filesystem and is not shared across machines or team members.
- **The DB is gitignored.** `tusk/tasks.db` (and `tusk/tasks.db-wal`, `tusk/tasks.db-shm`) are excluded from version control by design. Each developer has their own independent task database.
- **There is no built-in sync or import path.** No replication, rsync, or cross-machine restore workflow is provided.

**Workaround — export your tasks to SQL:**

```bash
sqlite3 tusk/tasks.db .dump > tasks.sql
```

This produces a portable SQL dump you can store, share, or use as a backup. There is currently no `tusk import` command; restoring from a dump requires running the SQL directly against a fresh database (e.g. `sqlite3 tusk/tasks.db < tasks.sql`). Prefer this only as a last resort — re-running `tusk init` and re-creating tasks is the supported recovery path.

---

## Entities

### Task

The core unit of work. Every piece of planned work is a task.

| Attribute | Type | Constraints | Description |
|-----------|------|-------------|-------------|
| `id` | INTEGER | PK, autoincrement | Stable identifier; never reused |
| `summary` | TEXT | NOT NULL | One-line description |
| `description` | TEXT | nullable | Full context, requirements, acceptance notes |
| `status` | TEXT | validated; default `To Do` | Lifecycle stage (see Status Transitions) |
| `priority` | TEXT | validated; default `Medium` | Relative importance (Highest → Lowest) |
| `domain` | TEXT | validated if config non-empty | Functional area (e.g., cli, db, docs) |
| `assignee` | TEXT | validated if config non-empty | Agent or person responsible |
| `task_type` | TEXT | validated if config non-empty | Category of work (see [Task Type Semantics](#task-type-semantics)) |
| `priority_score` | INTEGER | default 0 | WSJF score; recomputed by `tusk wsjf` |
| `expires_at` | TEXT | nullable | ISO datetime; task auto-closed when past this date |
| `closed_reason` | TEXT | validated; required when status=Done | Why the task was closed |
| `complexity` | TEXT | validated if config non-empty | T-shirt size estimate (XS, S, M, L, XL) |
| `is_deferred` | INTEGER | CHECK IN (0, 1); NOT NULL DEFAULT 0 | 1 if this is a deferred task (set by `tusk task-insert --deferred` or when summary starts with `[Deferred]`). Excluded from `v_ready_tasks` and `v_chain_heads` so deferred tasks never surface as ready-to-work or chain entry points (added in migration 59) |
| `workflow` | TEXT | nullable; validated if config non-empty | Custom workflow skill name; when set, `/tusk` and `/chain` route to `.claude/skills/<workflow>/SKILL.md` instead of the default dev cycle |
| `created_at` | TEXT | default now | Creation timestamp |
| `updated_at` | TEXT | default now | Last-modified timestamp |
| `started_at` | TEXT | nullable | When the task first moved to In Progress; set by `tusk task-start`, backfilled from `MIN(task_sessions.started_at)` by migration 36 |
| `closed_at` | TEXT | nullable | When the task was closed; set by `tusk task-done`, backfilled from `updated_at` for existing Done tasks by migration 37. Used by `v_velocity` for accurate week bucketing |
| `fixes_task_id` | INTEGER | nullable; FK → `tasks(id)` ON DELETE SET NULL | ID of the source task this is a follow-up/rework of. Set by `tusk task-insert --fixes-task-id <id>` or by `/create-task` when the input says "fixes TASK-N", "follow-up from TASK-N", or "retro follow-up from TASK-N". Added in migration 55 with a best-effort backfill over existing task descriptions and git-log commit bodies |
| `bakeoff_id` | INTEGER | nullable | Shared id grouping a set of shadow attempts produced by `tusk bakeoff`. A single bake-off run clones the source task N times (one per model) and stamps every shadow row with the same `bakeoff_id` so the parallel attempts can be aggregated into the comparison report. Added in migration 58 |
| `bakeoff_shadow` | INTEGER | CHECK IN (0, 1); NOT NULL DEFAULT 0 | 1 for rows created by `tusk bakeoff` as throwaway per-model attempts; 0 for normal tasks. Every view that projects `tasks` (`task_metrics`, `v_ready_tasks`, `v_chain_heads`, `v_blocked_tasks`, `v_criteria_coverage`) filters on `WHERE t.bakeoff_shadow = 0` so shadows never leak into ready/chain-head/blocked/coverage aggregates; the default `tusk task-list` output also hides them. Added in migration 58 |

**Canonical values:**
- `status`: `To Do`, `In Progress`, `Done`
- `priority`: `Highest`, `High`, `Medium`, `Low`, `Lowest`
- `closed_reason`: `completed`, `expired`, `wont_do`, `duplicate`
- `complexity`: `XS` (~1 quick session), `S` (~1 full session), `M` (~1–2 sessions), `L` (~3–5 sessions), `XL` (~5+ sessions)

#### Rework Attribution

`fixes_task_id` lets post-hoc rollups ask: *did the code a given model shipped actually stick, or did it need to be re-fixed?* Joining follow-up tasks back to the sessions that originally closed their source tasks produces a per-model rework rate:

```sql
-- Fraction of each model's closed feature/bug tasks that later had a follow-up
-- task created against them. Lower is better.
WITH closer_sessions AS (
    SELECT s.task_id,
           s.model,
           ROW_NUMBER() OVER (PARTITION BY s.task_id ORDER BY s.ended_at DESC) AS rn
      FROM task_sessions s
     WHERE s.ended_at IS NOT NULL
)
SELECT cs.model,
       COUNT(DISTINCT t.id) AS shipped_tasks,
       COUNT(DISTINCT fu.id) AS rework_tasks,
       ROUND(1.0 * COUNT(DISTINCT fu.id) / NULLIF(COUNT(DISTINCT t.id), 0), 3) AS rework_rate
  FROM tasks t
  JOIN closer_sessions cs ON cs.task_id = t.id AND cs.rn = 1
  LEFT JOIN tasks fu ON fu.fixes_task_id = t.id
 WHERE t.status = 'Done'
   AND t.closed_reason = 'completed'
   AND t.task_type IN ('feature', 'bug')
 GROUP BY cs.model
 ORDER BY rework_rate ASC;
```

The `closer_sessions` CTE picks the session that actually shipped each task (the most recently ended session, since tasks often span multiple sessions); the outer query attributes any follow-up that links back via `fixes_task_id` to that closer model. Apply the `COALESCE(NULLIF(model, ''), 'unknown')` normalization from the data-access layer if you want NULL and empty-string models bucketed together.

#### Task Type Semantics

The core question when choosing a `task_type` is: **is this a deliverable, or a proof of completeness?**

- **Deliverable** → the work itself belongs in a task.
- **Proof of completeness** → it belongs as an acceptance criterion on an existing task, not as its own task.

| task_type | When to use as a task | Task-vs-criterion guidance |
|---|---|---|
| `feature` | New user-facing capability or behaviour | A feature is always a task deliverable. Tests that verify the feature are criteria on that task, not separate tasks. |
| `bug` | Fix incorrect or unintended behaviour | The fix is the deliverable task. A failing test that reproduces the bug is a criterion, not a separate task. |
| `refactor` | Improve code structure without changing external behaviour | Refactors are deliverable tasks. Verifying that existing tests still pass is a criterion, not a separate task. |
| `infrastructure` | CI, tooling, deployment, or environment changes | Infrastructure work is a task deliverable. A smoke-test confirming the pipeline works is a criterion. |
| `test` | *Create or significantly overhaul a test suite as the primary goal.* Use this only when writing tests is itself the deliverable (e.g. adding coverage to a previously-untested module). **Do not use `test` for tests written as proof-of-completeness for another task** — those are acceptance criteria (`criterion_type = test`) on the parent task. |
| `docs` | *Create or significantly update documentation as the primary goal.* Use this only when the documentation is itself the deliverable (e.g. writing a new DOMAIN.md section, a user guide, or an ADR). **Do not use `docs` for a docstring, inline comment, or changelog entry added while completing another task** — those are acceptance criteria on the parent task. |

**Summary rule:** If the work *directly delivers* the outcome the user asked for, make it a task. If the work *verifies* that an outcome was achieved, make it a criterion (`criterion_type = test`, `code`, `file`, or `manual`) on the owning task.

---

### Acceptance Criterion

A verifiable condition that must be satisfied before a task is considered done. Tasks have zero or more criteria.

| Attribute | Type | Constraints | Description |
|-----------|------|-------------|-------------|
| `id` | INTEGER | PK, autoincrement | |
| `task_id` | INTEGER | FK → tasks(id) CASCADE | Owning task |
| `criterion` | TEXT | NOT NULL | Human-readable condition |
| `source` | TEXT | CHECK IN (original, subsumption, pr_review) | How this criterion was added |
| `is_completed` | INTEGER | CHECK IN (0, 1); default 0 | Whether the criterion has been met |
| `completed_at` | TEXT | nullable | When it was marked done |
| `cost_dollars` | REAL | nullable | AI cost accrued to complete this criterion |
| `tokens_in` | INTEGER | nullable | Input tokens used |
| `tokens_out` | INTEGER | nullable | Output tokens used |
| `criterion_type` | TEXT | CHECK IN (manual, code, test, file) | Verification method |
| `verification_spec` | TEXT | nullable | Shell command (code/test) or glob pattern (file) |
| `verification_result` | TEXT | nullable | Output captured from verification run |
| `commit_hash` | TEXT | nullable | Commit that satisfied this criterion |
| `committed_at` | TEXT | nullable | When that commit was made |
| `is_deferred` | INTEGER | CHECK IN (0, 1); default 0 | Criterion deferred to a downstream chain task |
| `deferred_reason` | TEXT | nullable | Why it was deferred |
| `skip_note` | TEXT | nullable | Rationale recorded when closed via `tusk criteria done --skip-verify --note "…"` |
| `created_at` | TEXT | default now | |
| `updated_at` | TEXT | default now | |

**Criterion types:**
- `manual` — verified by human judgment; no automated check
- `code` — verified by running a shell command; blocks completion on failure unless `--skip-verify`
- `test` — same as code; distinguished for reporting
- `file` — verified by checking a glob pattern exists on disk

**`code`/`test` auto-exclusions for grep.** Every `code`/`test` spec is prefixed with a POSIX shell function that redefines `grep` to add `--exclude-dir=__pycache__ --exclude-dir=.pytest_cache --exclude-dir=node_modules`. `grep -r` ignores `.gitignore`, so a spec like `! grep -rE "foo" skills/` would otherwise match `foo` inside compiled `.pyc` bytecode or cached dependency trees and fail the negation. The exclusions are a no-op for non-recursive grep and don't affect non-grep specs. If you need to grep *inside* one of those dirs, call `command grep` directly to bypass the wrapper.

**Verification subprocess timeouts.** `code`-type specs run under a 120s subprocess timeout; `test`-type specs get 300s because `subprocess.run(capture_output=True)` can slow pytest invocations ~2.5x vs direct runs. On failure, the captured output is prepended with `exit_code=<N>, elapsed=<Xs>\n` so non-zero exits are distinguishable from timeouts (which report `exit_code=timeout`). The metadata header survives the 2000-char output truncation.

**Sources:**
- `original` — specified when task was created
- `subsumption` — added when a duplicate task was merged in
- `pr_review` — added by a code reviewer during review

---

### Task Dependency

A directed edge from one task to another expressing that one task must be done before another can start.

| Attribute | Type | Constraints | Description |
|-----------|------|-------------|-------------|
| `task_id` | INTEGER | FK → tasks(id) CASCADE; part of PK | The task that depends on another |
| `depends_on_id` | INTEGER | FK → tasks(id) CASCADE; part of PK | The prerequisite task |
| `relationship_type` | TEXT | CHECK IN (blocks, contingent); default blocks | Strength of the dependency |
| `created_at` | TEXT | default now | |

**Constraints:**
- `task_id != depends_on_id` (no self-loops, enforced by CHECK)
- No cycles (enforced by DFS in `tusk-deps.py` before INSERT)

See [Relationship Semantics](#relationship-semantics-blocks-vs-contingent) for the difference between `blocks` and `contingent`.

---

### External Blocker

An obstacle outside the task graph — waiting for data, approval, infrastructure, or a third party — that prevents a task from being ready even if all dependencies are complete.

| Attribute | Type | Constraints | Description |
|-----------|------|-------------|-------------|
| `id` | INTEGER | PK, autoincrement | |
| `task_id` | INTEGER | FK → tasks(id) CASCADE | Blocked task |
| `description` | TEXT | NOT NULL | What is blocking progress |
| `blocker_type` | TEXT | validated if config non-empty | Category of the blocker |
| `is_resolved` | INTEGER | CHECK IN (0, 1); default 0 | Whether the blocker has been cleared |
| `created_at` | TEXT | default now | |
| `resolved_at` | TEXT | nullable | When `tusk blockers resolve` was called |

**Blocker types:** `data`, `approval`, `infra`, `external`

A task with any open (unresolved) external blocker is excluded from `v_ready_tasks` and `v_chain_heads`.

---

### Task Session

A bounded work session on a task, tracking cost and metrics. A task can have multiple sessions across multiple days or agents.

| Attribute | Type | Constraints | Description |
|-----------|------|-------------|-------------|
| `id` | INTEGER | PK, autoincrement | |
| `task_id` | INTEGER | FK → tasks(id) | Owning task |
| `started_at` | TEXT | NOT NULL | When work began |
| `ended_at` | TEXT | nullable | When the session was closed |
| `duration_seconds` | INTEGER | nullable | Wall-clock time |
| `cost_dollars` | REAL | nullable | AI API cost |
| `tokens_in` | INTEGER | nullable | Input tokens |
| `tokens_out` | INTEGER | nullable | Output tokens |
| `lines_added` | INTEGER | nullable | Git diff lines added |
| `lines_removed` | INTEGER | nullable | Git diff lines removed |
| `model` | TEXT | nullable | Claude model ID used |
| `agent_name` | TEXT | nullable | Named agent that ran the session (e.g. set by /chain when spawning parallel agents) |
| `peak_context_tokens` | INTEGER | nullable | Peak context window usage observed during the session |
| `first_context_tokens` | INTEGER | nullable | Context window size at the start of the session |
| `last_context_tokens` | INTEGER | nullable | Context window size at the end of the session |
| `context_window` | INTEGER | nullable | Total context window capacity for the model used (e.g. 1000000 for claude-sonnet-4-6); used as denominator in ctx_pct calculations |
| `request_count` | INTEGER | nullable | Deduplicated Claude API request count for the session (distinct requestIds in the transcript time window); populated by `tusk session-stats` / `tusk session-recalc` |

**Invariant:** At most one open (unclosed) session per task is allowed. Enforced by a partial UNIQUE index: `UNIQUE INDEX idx_task_sessions_open ON task_sessions(task_id) WHERE ended_at IS NULL`. `tusk task-start` detects a concurrent-insert race via `IntegrityError` and reuses the winning session with a warning rather than failing.

---

### Task Status Transition

An append-only audit log entry recording one change of `tasks.status`. Populated automatically by the `log_task_status_transition` trigger so that rework — a task that cycles `In Progress → To Do → In Progress` or is reopened `Done → To Do` via `tusk task-reopen --force` — is distinguishable from a task that moved straight through the lifecycle. `tasks.status` only holds the current state; without this log, "how many times did this task get reopened?" is unanswerable.

| Attribute | Type | Constraints | Description |
|-----------|------|-------------|-------------|
| `id` | INTEGER | PK, autoincrement | |
| `task_id` | INTEGER | FK → tasks(id) ON DELETE CASCADE | Owning task |
| `from_status` | TEXT | nullable | Previous status (NULL only for synthetic backfill rows with no prior state) |
| `to_status` | TEXT | NOT NULL | New status |
| `changed_at` | TEXT | NOT NULL, default now | Timestamp of the transition |

**Indexes:** `idx_task_status_transitions_task_id`.

**Trigger: `log_task_status_transition`.** `AFTER UPDATE OF status ON tasks FOR EACH ROW WHEN OLD.status IS NOT NEW.status` — inserts one row per status change. No-op UPDATEs (same-status assignments) do not log. The trigger fires for transitions performed via `tusk task-reopen --force` too: that command drops and recreates `validate_status_transition` (the forward-only enforcement trigger), not `log_task_status_transition`, so reopens are captured.

**Backfill.** Migration 53 seeds synthetic rows for existing tasks so the table is not empty on first upgrade:
- **Done** tasks get a `'To Do' → 'In Progress'` row at `started_at` (when set) plus an `'In Progress' → 'Done'` row at `COALESCE(closed_at, updated_at)`.
- **In Progress** tasks get a `'To Do' → 'In Progress'` row at `started_at` (when set).
- **To Do** tasks get nothing.

**No historical reopen recovery.** No synthetic row ever has `to_status = 'To Do'` (the predicate `task_metrics.reopen_count` counts, as of migration 54), because reopen and rework history were never stored in the DB or git. `task_metrics.reopen_count` is therefore always `0` for tasks that were already closed before migration 53 landed; the audit trail is forward-looking only. See `docs/MIGRATIONS.md § Seeding Audit Tables: No Historical Recovery`.

---

### Task Progress Checkpoint

An append-only log entry written after each commit, capturing enough context for a new agent to resume work mid-task without reading the full conversation history.

| Attribute | Type | Constraints | Description |
|-----------|------|-------------|-------------|
| `id` | INTEGER | PK, autoincrement | |
| `task_id` | INTEGER | FK → tasks(id) CASCADE | Owning task |
| `commit_hash` | TEXT | nullable | SHA of the commit triggering this checkpoint |
| `commit_message` | TEXT | nullable | Commit message |
| `files_changed` | TEXT | nullable | Newline-separated list of changed files |
| `next_steps` | TEXT | nullable | Free-text brief for the next agent |
| `created_at` | TEXT | default now | |

Written by `tusk progress <task_id> --next-steps "..."`. Read back by `tusk task-start` and the `/resume-task` skill.

---

### Code Review

One reviewer's assessment of a task's PR, for one pass of the fix-and-re-review cycle.

| Attribute | Type | Constraints | Description |
|-----------|------|-------------|-------------|
| `id` | INTEGER | PK, autoincrement | |
| `task_id` | INTEGER | FK → tasks(id) CASCADE | Reviewed task |
| `reviewer` | TEXT | nullable | Reviewer name (from config) |
| `status` | TEXT | CHECK IN (pending, in_progress, approved, changes_requested, superseded) | Review outcome. `superseded` is set automatically by `tusk review start` when prior pending reviews exist for the same task — they are superseded before the new review begins. |
| `review_pass` | INTEGER | default 1 | Which fix-and-re-review iteration (1 = first review) |
| `diff_summary` | TEXT | nullable | Summary of the diff being reviewed |
| `cost_dollars` | REAL | nullable | AI cost of this review pass |
| `tokens_in` | INTEGER | nullable | |
| `tokens_out` | INTEGER | nullable | |
| `agent_name` | TEXT | nullable | Named agent that ran the review (set by /chain when spawning an agent-driven review) |
| `model` | TEXT | nullable | Model that produced the review (e.g. `claude-opus-4-7`); set by `tusk review approve --model` / `tusk review request-changes --model` on review close. Added in migration 52; historical rows are backfilled from `task_sessions.model` by joining `code_reviews.created_at` into session windows. |
| `note` | TEXT | nullable | Optional reason or note stored with the approval (e.g. "Auto-approved (stall): agent exceeded monitoring threshold") |
| `created_at` | TEXT | default now | |
| `updated_at` | TEXT | default now | |

---

### Review Comment

An individual finding within a code review, with its own resolution lifecycle.

`resolution` encodes the *outcome* of handling the finding — not its state. A NULL resolution means the finding is open (unresolved). Once the developer acts, it gets exactly one of three outcome values:

- **fixed** — addressed immediately in the current session
- **deferred** — too large or out of scope; a follow-up task is created (`deferred_task_id` is set)
- **dismissed** — intentionally skipped with a documented reason

This model keeps `resolution` semantically pure: it only holds an outcome type, never a status placeholder. Open vs. resolved is determined by `IS NULL` / `IS NOT NULL`.

| Attribute | Type | Constraints | Description |
|-----------|------|-------------|-------------|
| `id` | INTEGER | PK, autoincrement | |
| `review_id` | INTEGER | FK → code_reviews(id) CASCADE | Owning review |
| `file_path` | TEXT | nullable | File the finding applies to |
| `line_start` | INTEGER | nullable | Starting line |
| `line_end` | INTEGER | nullable | Ending line |
| `category` | TEXT | validated if config non-empty | Finding category (must_fix, suggest, defer) |
| `severity` | TEXT | validated if config non-empty | Finding severity (critical, major, minor) |
| `comment` | TEXT | NOT NULL | The finding text |
| `resolution` | TEXT | nullable; CHECK IN (fixed, deferred, dismissed) | Outcome when resolved; NULL = open/unresolved |
| `deferred_task_id` | INTEGER | FK → tasks(id); nullable | Task created when a finding is deferred |
| `created_at` | TEXT | default now | |
| `updated_at` | TEXT | default now | |

---

### Skill Run

A record of a single execution of a tusk skill, capturing start/end timestamps, token usage, and estimated cost. Used to track operational cost of maintenance operations like `/groom-backlog` over time.

| Attribute | Type | Constraints | Description |
|-----------|------|-------------|-------------|
| `id` | INTEGER | PK, autoincrement | |
| `skill_name` | TEXT | NOT NULL | Skill invoked (e.g. `groom-backlog`) |
| `started_at` | TEXT | NOT NULL, default now | When `tusk skill-run start` was called |
| `ended_at` | TEXT | nullable | Set by `tusk skill-run finish` or `tusk skill-run cancel` |
| `cost_dollars` | REAL | nullable | Estimated cost from transcript parsing |
| `tokens_in` | INTEGER | nullable | Total input tokens (base + cache write + cache read) |
| `tokens_out` | INTEGER | nullable | Output tokens |
| `model` | TEXT | nullable | Dominant model used during the run |
| `metadata` | TEXT | nullable | JSON blob with skill-specific stats (e.g. tasks_done, tasks_deleted) |
| `request_count` | INTEGER | nullable | Deduplicated Claude API request count for the run (distinct requestIds in the transcript time window); populated by `tusk skill-run finish`, zeroed by `tusk skill-run cancel` |
| `task_id` | INTEGER | FK → tasks(id) ON DELETE SET NULL; nullable | Originating task for task-scoped skills (e.g. `/review-commits`); set from `--task-id <id>` on `tusk skill-run start`. NULL for standalone skills like `/groom-backlog` and `/tusk-insights` that don't run against a specific task |

---

### Retro Finding

One row per approved finding emitted by `/retro` on close. Populated by the skill via a direct `INSERT` after the user approves a finding for action (task created, lint rule added, convention recorded, skill patched inline, or documented). Read by `tusk retro-themes` to surface themes (grouped by `category`) that recur across recent retros — the cross-retro pattern detector that a single retrospective can't see.

| Attribute | Type | Constraints | Description |
|-----------|------|-------------|-------------|
| `id` | INTEGER | PK, autoincrement | |
| `skill_run_id` | INTEGER | NOT NULL, FK → skill_runs(id) ON DELETE CASCADE | The retro run that produced this finding; cascades so findings disappear if the originating run is deleted |
| `task_id` | INTEGER | nullable, FK → tasks(id) ON DELETE SET NULL | The task the retro was reviewing (`RETRO_TASK_ID`). Null when retro ran without a task context. SET NULL on delete — findings outlive their origin task so the cross-retro history survives task cleanup |
| `category` | TEXT | NOT NULL | Finding category — defaults are `A`/`B`/`C`/`D`/`E` (see `skills/retro/SKILL.md` Step 3), or a custom label when `FOCUS.md` is in use. Grouped on by `tusk retro-themes` as the "theme" |
| `summary` | TEXT | NOT NULL | One-line title of the finding as presented to the user for approval |
| `action_taken` | TEXT | nullable | What `/retro` did with the finding — `task:TASK-N` when a task was created, `issue:<url>` when a GitHub issue was filed, `convention:<id>`, `lint:<id>`, `skill-patch:<file>`, or `documented` for CLAUDE.md/skill inline edits. Null when the finding was approved for tracking but no concrete action was recorded |
| `created_at` | TEXT | NOT NULL, default now | When the finding was recorded (at retro close time) |

**Indexes:** `idx_retro_findings_skill_run_id`, `idx_retro_findings_task_id`, `idx_retro_findings_category`, `idx_retro_findings_created_at`.

---

### Tool Call Stats

Pre-computed per-tool-call cost aggregates, grouped by session, skill run, or criterion and tool name. Populated by `tusk call-breakdown` which parses Claude transcripts and summarises which tools were called most/least expensively. Each row belongs to exactly one of: a session, a skill run, or a criterion — never more than one, never none.

| Attribute | Type | Constraints | Description |
|-----------|------|-------------|-------------|
| `id` | INTEGER | PK, autoincrement | |
| `session_id` | INTEGER | nullable, FK → task_sessions(id) ON DELETE CASCADE | Owning session (set for session rows, NULL for skill-run/criterion rows) |
| `task_id` | INTEGER | nullable, FK → tasks(id) ON DELETE SET NULL | Denormalised task reference for convenient joins |
| `skill_run_id` | INTEGER | nullable, FK → skill_runs(id) ON DELETE CASCADE | Owning skill run (set for skill-run rows, NULL for session/criterion rows) |
| `criterion_id` | INTEGER | nullable, FK → acceptance_criteria(id) ON DELETE CASCADE | Owning criterion (set for criterion rows, NULL for session/skill-run rows) |
| `tool_name` | TEXT | NOT NULL | Name of the Claude tool (e.g. `Bash`, `Read`, `Edit`) |
| `call_count` | INTEGER | NOT NULL, default 0 | Number of invocations of this tool in the window |
| `total_cost` | REAL | NOT NULL, default 0.0 | Summed estimated cost across all calls |
| `max_cost` | REAL | NOT NULL, default 0.0 | Cost of the single most expensive call |
| `tokens_out` | INTEGER | NOT NULL, default 0 | Total output tokens attributed to this tool |
| `tokens_in` | INTEGER | NOT NULL, default 0 | Total input tokens attributed to this tool |
| `computed_at` | TEXT | NOT NULL, default now | When this aggregate row was written |

**Constraints:**
- `UNIQUE (session_id, tool_name)` — at most one aggregate row per tool per session (upsert safe).
- `UNIQUE (skill_run_id, tool_name)` — at most one aggregate row per tool per skill run (upsert safe).
- `UNIQUE (criterion_id, tool_name)` — at most one aggregate row per tool per criterion (upsert safe).
- `CHECK (session_id IS NOT NULL OR skill_run_id IS NOT NULL OR criterion_id IS NOT NULL)` — every row must have at least one parent; orphaned rows are rejected.

**Indexes:** `idx_tool_call_stats_session_id`, `idx_tool_call_stats_task_id`, `idx_tool_call_stats_skill_run_id`, `idx_tool_call_stats_criterion_id`.

---

### Tool Call Events

Individual per-call rows recording one entry per tool invocation within a session, criterion, or skill-run time window. Unlike `tool_call_stats` which aggregates per-tool per-window, `tool_call_events` preserves the full timeline with ordering (`call_sequence`) and timestamps (`called_at`). Populated by `tusk call-breakdown` alongside the aggregate writes. Each row belongs to exactly one of: a session, a criterion, or a skill run — never none.

| Attribute | Type | Constraints | Description |
|-----------|------|-------------|-------------|
| `id` | INTEGER | PK, autoincrement | |
| `task_id` | INTEGER | nullable, FK → tasks(id) ON DELETE SET NULL | Denormalised task reference for convenient joins |
| `session_id` | INTEGER | nullable, FK → task_sessions(id) ON DELETE CASCADE | Owning session (set for session rows, NULL otherwise) |
| `criterion_id` | INTEGER | nullable, FK → acceptance_criteria(id) ON DELETE CASCADE | Owning criterion (set for criterion rows, NULL otherwise) |
| `skill_run_id` | INTEGER | nullable, FK → skill_runs(id) ON DELETE CASCADE | Owning skill run (set for skill-run rows, NULL otherwise) |
| `tool_name` | TEXT | NOT NULL | Name of the Claude tool (e.g. `Bash`, `Read`, `Edit`) |
| `cost_dollars` | REAL | NOT NULL, default 0.0 | Estimated cost for this individual call |
| `tokens_in` | INTEGER | NOT NULL, default 0 | Input tokens attributed to this call |
| `tokens_out` | INTEGER | NOT NULL, default 0 | Output tokens attributed to this call |
| `call_sequence` | INTEGER | NOT NULL, default 0 | 1-based ordering of this call within the window |
| `called_at` | TEXT | NOT NULL | ISO 8601 timestamp of the assistant message containing this tool use |

**Constraints:**
- `CHECK (session_id IS NOT NULL OR criterion_id IS NOT NULL OR skill_run_id IS NOT NULL)` — every row must be attributed to a session, criterion, or skill run; fully-orphaned rows are rejected.

**Indexes:** `idx_tool_call_events_session_id`, `idx_tool_call_events_task_id`, `idx_tool_call_events_criterion_id`, `idx_tool_call_events_skill_run_id`.

---

### Convention

A generalizable heuristic or rule captured for the project. Managed via `tusk conventions add|list|search|remove`. Use `--topics` to tag conventions for filtering and search.

| Attribute | Type | Constraints | Description |
|-----------|------|-------------|-------------|
| `id` | INTEGER | PK, autoincrement | |
| `text` | TEXT | NOT NULL | Convention body, optionally starting with `## <title>` heading |
| `source_skill` | TEXT | nullable | Skill that wrote this convention (e.g. `retro`) |
| `lint_rule` | TEXT | nullable | Associated lint rule identifier, if any |
| `violation_count` | INTEGER | NOT NULL, default 0 | Number of times this convention has been violated (for future lint integration) |
| `qualitative` | INTEGER | NOT NULL, default 0 | Whether this convention is qualitative (not grep-detectable); set via `--qualitative` flag |
| `topics` | TEXT | nullable | Comma-separated topic tags for filtering and search (e.g. `zsh,cli,git`) |
| `created_at` | TEXT | default now | When the convention was written |

---

### Lint Rule

A DB-backed grep rule that `tusk lint` runs alongside its hardcoded rules. Rules are managed via `tusk lint-rule add/list/remove` and created by skills or users. Advisory rules emit `WARN [ADVISORY]`; blocking rules contribute to the non-zero exit code.

| Attribute | Type | Constraints | Description |
|-----------|------|-------------|-------------|
| `id` | INTEGER | PK, autoincrement | Stable identifier |
| `grep_pattern` | TEXT | NOT NULL | Extended regex pattern (`grep -E`) to search for |
| `file_glob` | TEXT | NOT NULL | Glob pattern for files to search (e.g. `**/*.py`, `skills/**/*.md`) |
| `message` | TEXT | NOT NULL | Violation message shown when the pattern is found |
| `is_blocking` | INTEGER | CHECK IN (0, 1); NOT NULL, default 0 | 1 = counts toward lint exit code; 0 = advisory warning only |
| `source_skill` | TEXT | nullable | Skill that created this rule (e.g. `lint-conventions`) |
| `created_at` | TEXT | default now | When the rule was added |

---

### Pillar

A named design principle with a one-sentence core claim. Managed via `tusk pillars list|add|remove|set-claim|sync-from-md`. The `pillars` table is a **normalized projection** of `docs/PILLARS.md` — the markdown file is the canonical narrative source (definitions, maturity, representative features), while the table indexes `name` and `core_claim` for machine queries by `/investigate`, `/investigate-directory`, and `/address-issue`. `tusk pillars sync-from-md` performs an idempotent upsert from the doc; migration 47 and `tusk init` seed the table automatically when `docs/PILLARS.md` is present. Target projects without a PILLARS.md fall back to `/tusk-init`'s catalogue-based seeding.

| Attribute | Type | Constraints | Description |
|-----------|------|-------------|-------------|
| `id` | INTEGER | PK, autoincrement | Stable identifier |
| `name` | TEXT | NOT NULL, UNIQUE | Short pillar name (e.g. `portability`) |
| `core_claim` | TEXT | NOT NULL | One-sentence statement of what the pillar values |
| `created_at` | TEXT | default now | When the pillar was added |

---

## Status Transitions

Task `status` follows a one-way lifecycle. The `validate_status_transition` trigger (in `bin/tusk`, recreated by `tusk regen-triggers`) enforces this graph:

```
                  ┌─────────────┐
                  │    To Do    │
                  └──────┬──────┘
                         │
              ┌──────────┴──────────┐
              │                     │
              ▼                     ▼
       ┌─────────────┐       ┌─────────────┐
       │ In Progress │──────▶│    Done     │
       └─────────────┘       └─────────────┘
```

**Allowed transitions:**

| From | To | Notes |
|------|----|-------|
| `To Do` | `In Progress` | Normal start via `tusk task-start` |
| `To Do` | `Done` | Direct close for trivial/already-done tasks |
| `In Progress` | `Done` | Normal completion via `tusk task-done` |
| Any | Any (same) | No-op updates allowed |

**Blocked transitions (enforced by `validate_status_transition` trigger):**
- `Done` → anything (`Done` is terminal)
- `In Progress` → `To Do` (no reverting to unstarted)

**Escape hatch — `tusk task-reopen <task_id> --force`:** Deliberately bypasses the trigger to reset an In Progress or Done task back to `To Do`. It drops `validate_status_transition`, applies the UPDATE, then regenerates the trigger via `tusk regen-triggers`. All three operations run inside a single explicit transaction (using `BEGIN IMMEDIATE` with `isolation_level=None`) so the DB is never left in a partially-modified state. Use only for crash recovery, accidental status changes, or CI cleanup.

**Rule:** When setting `status = Done`, `closed_reason` MUST be set. The `validate_closed_reason` trigger enforces the value is from the config list.

---

## Invariant Table

Business rules and their enforcement mechanisms:

| Invariant | Enforcement | Location |
|-----------|-------------|----------|
| Status must be a valid config value | `validate_status` trigger (INSERT, UPDATE) | `bin/tusk` `generate_triggers()` |
| Priority must be a valid config value | `validate_priority` trigger | `bin/tusk` `generate_triggers()` |
| `closed_reason` must be valid when set | `validate_closed_reason` trigger | `bin/tusk` `generate_triggers()` |
| Domain must be valid (if config non-empty) | `validate_domain` trigger | `bin/tusk` `generate_triggers()` |
| Task type must be valid (if config non-empty) | `validate_task_type` trigger | `bin/tusk` `generate_triggers()` |
| Complexity must be valid (if config non-empty) | `validate_complexity` trigger | `bin/tusk` `generate_triggers()` |
| Blocker type must be valid (if config non-empty) | `validate_blocker_type` trigger | `bin/tusk` `generate_triggers()` |
| Criterion type must be valid (if config non-empty) | `validate_criterion_type` trigger | `bin/tusk` `generate_triggers()` |
| Review comment category must be valid | `validate_review_category` trigger | `bin/tusk` `generate_triggers()` |
| Review comment severity must be valid | `validate_review_severity` trigger | `bin/tusk` `generate_triggers()` |
| Status transition must follow the allowed graph | `validate_status_transition` trigger (BEFORE UPDATE) | `bin/tusk` `generate_triggers()` |
| No self-dependency (task cannot depend on itself) | `CHECK (task_id != depends_on_id)` on `task_dependencies` | Schema DDL in `bin/tusk` `cmd_init()` |
| No circular dependencies | DFS cycle check before INSERT | `bin/tusk-deps.py` |
| `relationship_type` must be `blocks` or `contingent` | `CHECK IN ('blocks', 'contingent')` on `task_dependencies` | Schema DDL in `bin/tusk` `cmd_init()` |
| `closed_reason` required when marking Done | Warning + non-zero exit unless `--force` | `bin/tusk-task-done.py` |
| Task must have acceptance criteria before start | Warning + non-zero exit unless `--force` | `bin/tusk-task-start.py` |
| All active criteria done before task closure | Warning + non-zero exit unless `--force` | `bin/tusk-task-done.py` |
| Non-`manual` criteria run automated verification on `done` | Shell exec (code/test) or glob check (file); blocks unless `--skip-verify` | `bin/tusk-criteria.py` |
| `closed_reason = duplicate` used for dupes | Convention enforced by skills | `tusk dupes check`, `/groom-backlog`, `/retro` |
| Deferred tasks have `is_deferred = 1`, `[Deferred]` prefix, and `expires_at` | `is_deferred` set by `tusk task-insert --deferred`; both prefix and column required | `bin/tusk-task-insert.py`, `skills/review-commits/SKILL.md` |

Config-driven triggers are regenerated from `config.json` by `tusk regen-triggers` and after each trigger-only migration. They enforce whatever values are in the config at regen time.

---

## Relationship Semantics: `blocks` vs `contingent`

Both types are expressed as rows in `task_dependencies` with different `relationship_type` values. Only `blocks`-type dependencies prevent the dependent task from appearing in `v_ready_tasks`. Contingent dependencies do not affect readiness — they are coordination signals, not hard prerequisites.

### `blocks` — Hard Dependency

Task A **blocks** Task B means: B logically cannot be started until A is complete. A is on the critical path to B.

- Used for: schema migrations before feature work, scaffold before consumers, data model before UI
- Priority effect: each downstream dependent task (of any relationship type) adds +5 to A's WSJF score (capped at +15), rewarding tasks that unblock the most work
- Auto-close: `tusk autoclose` does NOT auto-close tasks just because their `blocks` prerequisite is done — this is expected

### `contingent` — Soft Dependency

Task A **contingently blocks** Task B means: B can theoretically proceed, but it's better to wait for A. The relationship captures coordination intent, not logical necessity.

- Used for: "nice to have before starting", "reduces rework if done first", research before implementation
- Priority effect: if a task has ONLY contingent dependencies (no hard `blocks`), it receives a −10 WSJF penalty, pushing it below tasks with clearer critical-path value
- Auto-close: `tusk autoclose` closes "moot contingent tasks" — contingent tasks whose prereq was already resolved via another route. This prevents stale low-value tasks from lingering

### Summary

| | `blocks` | `contingent` |
|--|----------|--------------|
| Blocks readiness | Yes | No |
| WSJF bonus to prerequisite | +5 per downstream (max +15) | +5 per downstream (max +15) |
| WSJF penalty on dependent | None | −10 if only-contingent deps |
| Auto-close by `tusk autoclose` | No | Yes, if moot |
| Conceptual meaning | "Cannot proceed without" | "Better to wait, but not required" |

---

## Views

| View | Purpose | Used By |
|------|---------|---------|
| `task_metrics` | Aggregates session cost/tokens/lines/request_count per task (exposes `total_request_count` = SUM of `task_sessions.request_count`, plus `reopen_count` = count of `task_status_transitions` rows whose `to_status` is `'To Do'` — a forward-looking rework signal capturing both `In Progress → To Do` mid-task rework and `Done → To Do` post-Done reopens via `tusk task-reopen --force`; always `0` for tasks that were already closed before migration 53, since migration 53's backfill produces no `to_status='To Do'` rows). Projects `t.*` from `tasks`, so its column list is frozen at CREATE VIEW time; recreated in migration 56 to pick up `fixes_task_id` and again in migration 58 to pick up the `bakeoff_*` columns and apply `WHERE t.bakeoff_shadow = 0`. | `tusk-dashboard.py`, reporting |
| `v_ready_tasks` | Canonical "ready to work" definition: To Do, all `blocks`-type deps Done, no open external blockers (contingent deps do not prevent readiness), not a bake-off shadow, and not deferred. Projects `t.*`; recreated in migration 56 alongside `task_metrics`, in migration 58 to add the `bakeoff_shadow = 0` filter, and in migration 59 to add the `(is_deferred = 0 OR is_deferred IS NULL)` filter. | `/tusk`, `tusk-loop.py`, `tusk deps ready` |
| `v_chain_heads` | Non-Done tasks with unfinished downstream dependents and no unmet upstream deps, excluding bake-off shadows and deferred tasks. Projects `t.*`; recreated in migration 56, in migration 58 for the shadow filter, and in migration 59 for the deferred filter. | `/chain` |
| `v_blocked_tasks` | Non-Done tasks blocked by dependency or external blocker, with `block_reason` and `blocking_summary`. Excludes bake-off shadows (both UNION branches filter on `t.bakeoff_shadow = 0`) since the shadow's own dependency status is not meaningful for the parent backlog. | `/tusk blocked`, `tusk deps blocked` |
| `v_criteria_coverage` | Per-task counts of total, completed, and remaining criteria (deferred excluded). Projects specific `tasks` columns (not `t.*`), so ALTER additions do not silently drop out of its projection, but it was recreated in migration 56 to keep the set of tasks-dependent views uniform, and again in migration 58 to apply `WHERE t.bakeoff_shadow = 0`. | Reporting, `/tusk-insights` |
| `v_velocity` | Completed tasks (closed_reason=completed) grouped by calendar week (Mon-start, `%Y-W%W`) using `closed_at` (falls back to `updated_at`) with task_count, avg_cost, avg_tokens_in, avg_tokens_out | `/tusk-insights`, dashboard velocity card |

> **Compound blocking** — A task is _compound-blocked_ when it is held back by more than one simultaneous blocker (e.g., both an unfinished `blocks`-type dependency **and** an unresolved external blocker). Because `v_blocked_tasks` emits one row per blocking source, a compound-blocked task appears multiple times in the view. A task must clear **all** blocking sources before it surfaces in `v_ready_tasks`. See also: [`docs/GLOSSARY.md` — compound blocking](docs/GLOSSARY.md).

---

## Data Access Layer

`bin/tusk-dashboard-data.py` wraps the read-only queries that back the HTML dashboard (`tusk dashboard`). Most fetchers return plain lists of row dicts, but `fetch_model_performance` has a non-trivial return contract worth pinning here.

### `fetch_model_performance(conn, offset_minutes=0) → dict`

Backs the Models dashboard tab. Returns a four-key dict:

| Key | Shape | Grouping |
|-----|-------|----------|
| `models` | `list[dict]`, sorted by `task_cost + skill_cost` desc | One row per model. Merges `task_sessions` and `skill_runs` sub-aggregates so the Tasks/Skills/Both client-side toggle can recombine them. |
| `complexity_matrix` | `list[dict]` | One row per `(model, complexity)` bucket. Derived from `task_sessions ⨝ tasks` and filtered to `tasks.complexity IS NOT NULL` — `skill_runs` have no task linkage, hence no complexity. |
| `timeseries_tasks` | `list[dict]` | One row per `(day, model)` from `task_sessions`, bucketed by local date. |
| `timeseries_skills` | `list[dict]` | One row per `(day, model)` from `skill_runs`, bucketed by local date. Same shape as `timeseries_tasks`; `total_lines` is always `0` (skill_runs track no line counts). Empty list when the `skill_runs` table is absent on pre-migration DBs. |

`offset_minutes` shifts the `date()` bucket used by the two timeseries keys into the client's local timezone so the line chart aligns with the other time-series panels. It does not affect `models` or `complexity_matrix`.

**NULL vs zero `request_count`.** `task_request_count` / `skill_request_count` (in `models`), `avg_turns` (in `complexity_matrix`), and `request_count` (in both timeseries) are `None` — not `0` — when every contributing row has a NULL `request_count` (rows written before migration 49 added the column). The Models tab renders these as `—` so "unknown turns" stays visually distinct from a genuine zero.

### Model name normalization

Both `fetch_model_performance` and `fetch_cost_scatter_data` collapse NULL and empty-string `model` values into a single `'unknown'` bucket using:

```sql
COALESCE(NULLIF(model, ''), 'unknown')
```

Session-close paths have historically stamped both sentinels, and displaying two separate "unknown model" rows would be confusing. Any future data-access layer query that groups or displays by `task_sessions.model` or `skill_runs.model` should apply the same expression so the UI stays consistent.

---

## WSJF Priority Scoring

`priority_score` is the sort key for `v_ready_tasks`. Recomputed by `tusk wsjf`.

```
priority_score = ROUND(
  (base_priority + non_deferred_bonus + unblocks_bonus + contingent_adjustment) / complexity_weight
)
```

| Component | Value |
|-----------|-------|
| `base_priority` | Highest=100, High=80, Medium=60, Low=40, Lowest=20 |
| `non_deferred_bonus` | +10 if `is_deferred = 0`; 0 if `is_deferred = 1` (deferred tasks get no bonus) |
| `unblocks_bonus` | +5 per downstream dependent (any type), capped at +15 |
| `contingent_adjustment` | −10 if task has at least one `contingent` dependency and no `blocks` dependencies; 0 otherwise |
| `complexity_weight` (divisor) | XS=1, S=2, M=3, L=5, XL=8; default=3 if no complexity set |

---

## Config Validation

`config.json` drives which values are valid for several columns. The config is validated by `tusk validate` and `tusk init`. Trigger values are regenerated by `tusk regen-triggers`.

| Config key | Column controlled | Empty array behavior |
|------------|-------------------|----------------------|
| `statuses` | `tasks.status` | Required non-empty |
| `priorities` | `tasks.priority` | Required non-empty |
| `closed_reasons` | `tasks.closed_reason` | Required non-empty |
| `domains` | `tasks.domain` | Empty = no validation |
| `task_types` | `tasks.task_type` | Empty = no validation |
| `complexity` | `tasks.complexity` | Empty = no validation |
| `blocker_types` | `external_blockers.blocker_type` | Empty = no validation |
| `criterion_types` | `acceptance_criteria.criterion_type` | Empty = no validation |
| `review_categories` | `review_comments.category` | Empty = no validation |
| `review_severities` | `review_comments.severity` | Empty = no validation |

After editing `config.json` on an existing database, always run `tusk regen-triggers` — do NOT use `tusk init --force` (that drops the database).

---

## Configuration Schema

Non-enum config keys that control runtime behavior (not column validation). All live in `tusk/config.json`.

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `project_type` | `string \| null` | `null` | Selects the active entry in `project_libs`. Set by `/tusk-init` based on the project category (e.g. `ios_app`, `python_service`). `null` means no bootstrap seeding. |
| `project_libs` | `object` | (examples) | Maps project-type keys to `{ repo, ref }` objects. `repo` is an owner/name GitHub repo path; `ref` is a branch, tag, or commit SHA pinning which `tusk-bootstrap.json` to fetch. |
| `test_command` | `string` | `""` | Shell command used by `tusk commit` to run tests before committing. Empty string disables test gating. |
| `domain_test_commands` | `object` | `{}` | Map of `task.domain → shell command`. When the active task has a domain and a matching entry, `tusk commit` uses that command instead of the global `test_command`. |
| `path_test_commands` | `object` | `{}` | Map of `glob pattern → shell command`. Insertion order matters. Before falling back to `domain_test_commands` / `test_command`, `tusk commit` and `tusk test-precheck` pick the first pattern whose glob matches *every* staged/changed path. Users order patterns most-specific-first and append a `"*"` entry as an explicit catch-all. When staged paths span multiple patterns and no single pattern covers them all, resolution falls through (deterministic). Patterns use `fnmatch` semantics — `*` already matches across `/`. Absolute paths are normalized to repo-root-relative form before matching, so the configured patterns stay repo-relative regardless of how the caller spelled the path. An empty-string command disables that pattern — resolution falls through as if the entry were absent (same idiom as `domain_test_commands`). Resolution order: `path_test_commands` → `domain_test_commands` → `test_command`. |
| `review.mode` | `string` | `"ai_only"` | Controls AI code review. `"disabled"` skips review entirely; `"ai_only"` runs the configured reviewer. |
| `review.max_passes` | `integer` | `2` | Maximum review-fix cycles before `/review-commits` surfaces unresolved findings to the user. |
| `review.reviewer` | `object \| absent` | (general) | Single reviewer object with `name` and `description`. `/review-commits` spawns at most one background reviewer agent; absent means inline review only. |
| `dupes.check_threshold` | `float` | `0.82` | Similarity score above which a candidate is flagged as a likely duplicate during task insertion. |
| `dupes.similar_threshold` | `float` | `0.6` | Lower similarity threshold used for "possibly related" warnings (below `check_threshold`). |
| `dupes.strip_prefixes` | `array` | `["Deferred", ...]` | Prefixes stripped from task summaries before duplicate comparison (e.g. `[Deferred]` prefix added to PR-deferred tasks). |
| `merge.mode` | `string` | `"local"` | `"local"` fast-forward merges locally; `"pr"` squash-merges via `gh pr merge`. |

**`project_type` + `project_libs` relationship:** `project_type` is the lookup key into `project_libs`. When `/tusk-init` reaches the bootstrap step, it reads `project_libs[project_type]` to find the `repo` and `ref`, then fetches `tusk-bootstrap.json` from that repo at the pinned ref and seeds the listed tasks. If `project_type` is `null` or has no matching entry in `project_libs`, bootstrap seeding is skipped entirely. Pinning `ref` to a tag or commit SHA freezes which tasks get seeded, preventing unintended additions if the library repo's default branch changes later.

**`tusk-bootstrap.json` task format:** Each task object requires `summary`, `description`, `priority`, `task_type`, `complexity`, and `criteria` (non-empty array of strings). An optional `migration_hints` field (array of strings) may also be present. When a task is seeded via `/tusk-init` Step 8.5, each `migration_hints` entry is injected as an additional acceptance criterion prefixed with `[Migration]` (e.g., `"[Migration] Remove any ad-hoc logging.basicConfig() calls"`). Tasks without `migration_hints` (or with an empty array) are seeded identically to the current behavior.

**`tusk-bootstrap.json` contributor guide — adding `migration_hints`:**

`migration_hints` is intended for cleanup steps that a consuming project must perform *after* integrating the library — things that cannot be automated or verified by the library itself because they depend on the consuming project's existing code. Common examples: removing patterns the library replaces, ensuring conflicting packages are uninstalled, or verifying that old config files are deleted.

Each hint should be a self-contained imperative sentence that can stand alone as an acceptance criterion. It will appear verbatim (preceded by `[Migration]`) in the consuming project's task list, so write it as if briefing an engineer who has just added your library and needs a final checklist item.

**Text format guidelines:**
- Start with an action verb: *Remove*, *Delete*, *Verify*, *Ensure*, *Migrate*, *Update*
- Be specific enough to check: name the file, config key, or pattern to look for
- Keep each hint to one logical action — split compound actions into separate strings
- Avoid references to your library's own internals that the consuming project won't recognise

**Minimal `tusk-bootstrap.json` example with `migration_hints`:**

```json
{
  "version": 1,
  "project_type": "python_service",
  "tasks": [
    {
      "summary": "Install gioe-libs and configure structured logging",
      "description": "Add the gioe-libs package and replace ad-hoc logging setup with gioe_libs.aiq_logging.",
      "priority": "High",
      "task_type": "feature",
      "complexity": "S",
      "criteria": [
        "gioe-libs is listed in requirements.txt or pyproject.toml",
        "All modules call logging.getLogger(__name__) rather than basicConfig()"
      ],
      "migration_hints": [
        "Remove any top-level logging.basicConfig() calls — aiq_logging configures the root logger automatically",
        "Delete any manually created log handlers attached to the root logger before this integration"
      ]
    }
  ]
}
```

The two `migration_hints` entries above will be seeded as additional acceptance criteria:

```
[Migration] Remove any top-level logging.basicConfig() calls — aiq_logging configures the root logger automatically
[Migration] Delete any manually created log handlers attached to the root logger before this integration
```

These appear alongside the task's normal criteria in `tusk criteria list` and are treated identically — they must be checked off before the task can be marked done.
