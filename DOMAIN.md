# Tusk Domain Model

This document codifies the entity/attribute model, allowed status transitions, invariants, and relationship semantics for the tusk task management system. It is the authoritative reference for schema migrations, skill authoring, and AI context.

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
| `task_type` | TEXT | validated if config non-empty | Category (bug, feature, refactor, test, docs, infrastructure) |
| `priority_score` | INTEGER | default 0 | WSJF score; recomputed by `tusk wsjf` |
| `expires_at` | TEXT | nullable | ISO datetime; task auto-closed when past this date |
| `closed_reason` | TEXT | validated; required when status=Done | Why the task was closed |
| `complexity` | TEXT | validated if config non-empty | T-shirt size estimate (XS, S, M, L, XL) |
| `created_at` | TEXT | default now | Creation timestamp |
| `updated_at` | TEXT | default now | Last-modified timestamp |

**Canonical values:**
- `status`: `To Do`, `In Progress`, `Done`
- `priority`: `Highest`, `High`, `Medium`, `Low`, `Lowest`
- `closed_reason`: `completed`, `expired`, `wont_do`, `duplicate`
- `complexity`: `XS` (~1 quick session), `S` (~1 full session), `M` (~1–2 sessions), `L` (~3–5 sessions), `XL` (~5+ sessions)

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
| `created_at` | TEXT | default now | |
| `updated_at` | TEXT | default now | |

**Criterion types:**
- `manual` — verified by human judgment; no automated check
- `code` — verified by running a shell command; blocks completion on failure unless `--skip-verify`
- `test` — same as code; distinguished for reporting
- `file` — verified by checking a glob pattern exists on disk

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

**Invariant:** At most one open (unclosed) session per task is allowed. Enforced by a partial UNIQUE index: `UNIQUE INDEX idx_task_sessions_open ON task_sessions(task_id) WHERE ended_at IS NULL`. `tusk task-start` detects a concurrent-insert race via `IntegrityError` and reuses the winning session with a warning rather than failing.

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
| `status` | TEXT | CHECK IN (pending, in_progress, approved, changes_requested) | Review outcome |
| `review_pass` | INTEGER | default 1 | Which fix-and-re-review iteration (1 = first review) |
| `diff_summary` | TEXT | nullable | Summary of the diff being reviewed |
| `cost_dollars` | REAL | nullable | AI cost of this review pass |
| `tokens_in` | INTEGER | nullable | |
| `tokens_out` | INTEGER | nullable | |
| `agent_name` | TEXT | nullable | Named agent that ran the review (e.g. set by /chain when spawning parallel reviewers) |
| `created_at` | TEXT | default now | |
| `updated_at` | TEXT | default now | |

---

### Review Comment

An individual finding within a code review, with its own resolution lifecycle.

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
| `resolution` | TEXT | CHECK IN (pending, fixed, deferred, dismissed) | How the finding was handled |
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
| `ended_at` | TEXT | nullable | Set by `tusk skill-run finish` |
| `cost_dollars` | REAL | nullable | Estimated cost from transcript parsing |
| `tokens_in` | INTEGER | nullable | Total input tokens (base + cache write + cache read) |
| `tokens_out` | INTEGER | nullable | Output tokens |
| `model` | TEXT | nullable | Dominant model used during the run |
| `metadata` | TEXT | nullable | JSON blob with skill-specific stats (e.g. tasks_done, tasks_deleted) |

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
| `closed_reason = duplicate` used for dupes | Convention enforced by skills | `/check-dupes`, `/groom-backlog`, `/retro` |
| Deferred tasks get `[Deferred]` prefix and `expires_at` | Convention applied by `/review-commits` | `skills/review-commits/SKILL.md` |

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
| `task_metrics` | Aggregates session cost/tokens/lines per task | `tusk-dashboard.py`, reporting |
| `v_ready_tasks` | Canonical "ready to work" definition: To Do, all `blocks`-type deps Done, no open external blockers (contingent deps do not prevent readiness) | `/tusk`, `tusk-loop.py`, `tusk deps ready` |
| `v_chain_heads` | Non-Done tasks with unfinished downstream dependents and no unmet upstream deps | `/chain` |
| `v_blocked_tasks` | Non-Done tasks blocked by dependency or external blocker, with `block_reason` and `blocking_summary` | `/tusk blocked`, `tusk deps blocked` |
| `v_criteria_coverage` | Per-task counts of total, completed, and remaining criteria (deferred excluded) | Reporting, `/tusk-insights` |
| `v_velocity` | Completed tasks (closed_reason=completed) grouped by calendar week (Mon-start, `%Y-W%W`) with task_count, avg_cost, avg_tokens_in, avg_tokens_out | `/tusk-insights`, dashboard velocity card |

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
| `non_deferred_bonus` | +10 if summary does NOT contain `[Deferred]`; 0 if it does (deferred tasks get no bonus) |
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
