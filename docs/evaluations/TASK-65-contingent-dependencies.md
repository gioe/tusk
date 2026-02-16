# TASK-65: Evaluation — Contingent/Conditional Dependencies

## Problem Statement

The current `task_dependencies` table models only strict "A blocks B" gates. There is no way to express conditional relationships where the outcome of Task A determines whether Task B is needed at all.

**Real example:** "Evaluate whether heuristic dupe checker is needed" (TASK-64) may make "Remove redundant Step 3b from /retro" either necessary or obsolete depending on findings. Today you must either insert both and risk clutter, or hold one in someone's head.

This pattern is conceptually ubiquitous in software work — evaluation-spawns-implementation, investigation-determines-fix, prototype-picks-winner. The completion of one task routinely impacts the state and priority of others. The dependency model should capture this so that `/next-task` and `/groom-backlog` can make better automated decisions about what to work on next.

## Current State

- `task_dependencies` schema: `(task_id, depends_on_id, created_at)` — simple blocking edges
- 16 SQL queries across the codebase reference this table
- Cycle detection via DFS in Python (`manage_dependencies.py`)
- Self-loop prevention via CHECK constraint
- Touch points: `bin/tusk`, `scripts/manage_dependencies.py`, `/next-task`, `/groom-backlog`, `/manage-dependencies`

## Approaches Evaluated

### Option A: Dependency Type Column

Add a `relationship_type TEXT DEFAULT 'blocks'` column to `task_dependencies`.

Values: `blocks` (current behavior), `contingent` (outcome-dependent).

**Semantics:**
- Both types block — a `contingent` dep still prevents the downstream task from starting until the upstream task is Done (you shouldn't "implement X" before "evaluate X" finishes)
- The difference is what happens when the upstream task closes:
  - `blocks` + upstream closes as anything → downstream becomes ready (current behavior)
  - `contingent` + upstream closes as `completed` → downstream becomes ready (proceed)
  - `contingent` + upstream closes as `wont_do` or `expired` → downstream should be auto-closed as `wont_do` (the work is moot)

**Pros:**
- Minimal schema change — one column, backward-compatible default
- Simple migration (`ALTER TABLE ADD COLUMN` — no table recreation needed)
- Existing blocking queries unchanged — `NOT EXISTS` checks still work because both types block
- Clear, deterministic automation: `/groom-backlog` can cascade-close moot tasks without LLM judgment
- Priority scoring can factor in uncertainty: tasks with only contingent blockers are less certain to be needed
- `manage_dependencies.py` needs only an optional `--type` parameter

**Cons:**
- Adds a concept to the dependency model (but a well-bounded one — two values, clear semantics)
- `manage_dependencies.py` display commands should show the type for visibility
- `/groom-backlog` needs a new cascade-close step (but it's additive, not a rewrite)

**Migration complexity:** Low (ALTER TABLE ADD COLUMN)
**Behavior change complexity:** Low (blocking logic unchanged; new behavior is additive)
**Files affected:** 4 files need changes, most queries untouched

### Option B: Parent/Child Tasks

Add a `parent_task_id` column to `tasks`, allowing a task to spawn sub-tasks that only enter the backlog when the parent's outcome triggers them.

**Pros:**
- Cleanest conceptual model — contingent work doesn't exist until triggered
- Keeps the backlog free of speculative tasks

**Cons:**
- Significant schema change (new column on the `tasks` table, or a separate table)
- Major changes to `/groom-backlog`, `/next-task`, `/create-task` — they need to understand hierarchies
- Recursive queries for display (nested task trees)
- No clear triggering mechanism — what "spawns" the child? Manual? Automated on close?
- Essentially building a project management hierarchy — against tusk's flat-list philosophy

**Migration complexity:** High
**Behavior change complexity:** High
**Files affected:** 8+ files, new skill or command needed

### Option C: Notes/Metadata Only (Accept the Limitation)

Keep the current model. Document contingencies in task descriptions using natural language.

**Pros:**
- Zero code changes, zero migration, zero risk
- LLM agents reading task descriptions can reason about natural-language contingencies

**Cons:**
- No machine-readable signal — can't participate in priority scoring or auto-close cascades
- Relies entirely on LLM judgment during grooming to notice and act on textual contingencies
- Not robust enough for a pattern this common — stale/orphaned tasks will accumulate

**Migration complexity:** None
**Behavior change complexity:** None
**Files affected:** 0

### Option D: Status-Based Branching

Extend `closed_reason` or add an `outcome` field so downstream tasks can be auto-closed when a parent's outcome makes them moot.

**Pros:**
- Could enable automatic cascade closure of contingent work

**Cons:**
- `closed_reason` describes why a task closed (`completed`, `wont_do`), not what the task's output was — conflating these muddies both
- Requires a new concept of "task outcome" distinct from closure reason
- No obvious mapping: how does a downstream task declare "close me if TASK-X outcome is Y"? This needs a rule engine
- Highest conceptual complexity of all options for the least clear benefit

**Migration complexity:** Medium
**Behavior change complexity:** Very High
**Files affected:** 8+ files, new triggering infrastructure

## Recommendation: Option A — Dependency Type Column

**Add `relationship_type` to `task_dependencies` with two values: `blocks` and `contingent`.**

### Rationale

1. **The pattern is ubiquitous.** Evaluation-spawns-implementation, investigation-determines-fix, prototype-picks-winner — these conditional relationships appear constantly. A description convention isn't robust enough for something this common.

2. **Blocking semantics are unchanged.** Both `blocks` and `contingent` prevent the downstream task from starting. Existing `NOT EXISTS` queries work without modification. The new behavior is purely additive.

3. **Enables deterministic automation.** When a task with `contingent` dependents closes as `wont_do`, `/groom-backlog` can auto-close the dependents — no LLM judgment required. This prevents stale task accumulation.

4. **Improves task selection.** `/next-task` priority scoring can discount tasks whose upstream blockers are contingent (uncertain whether the work will be needed), biasing toward tasks with guaranteed value.

5. **Low implementation cost.** One `ALTER TABLE ADD COLUMN`, one new groom step, one optional `--type` flag. No table recreation, no query rewrites, no new tables.

## Implementation Sketch

### 1. Schema Migration (2→3)

Simple `ALTER TABLE ADD COLUMN` — no table recreation needed since we're adding a nullable column with a default.

**`bin/tusk` — `cmd_init()` schema definition:**
```sql
CREATE TABLE task_dependencies (
    task_id INTEGER NOT NULL,
    depends_on_id INTEGER NOT NULL,
    relationship_type TEXT DEFAULT 'blocks',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (task_id, depends_on_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (depends_on_id) REFERENCES tasks(id) ON DELETE CASCADE,
    CHECK (task_id != depends_on_id),
    CHECK (relationship_type IN ('blocks', 'contingent'))
);
```

**`bin/tusk` — `cmd_migrate()` new migration block:**
```bash
# Migration 2→3: add relationship_type column to task_dependencies
if [[ "$current" -lt 3 ]]; then
  sqlite3 "$DB_PATH" "
    ALTER TABLE task_dependencies
      ADD COLUMN relationship_type TEXT DEFAULT 'blocks'
        CHECK (relationship_type IN ('blocks', 'contingent'));
    PRAGMA user_version = 3;
  "
  echo "  Migration 3: added 'relationship_type' column to task_dependencies"
fi
```

Fresh DB version bumped from 2 to 3.

### 2. `scripts/manage_dependencies.py` Changes

**`DEPENDENCIES_SCHEMA`** — add column to `CREATE TABLE IF NOT EXISTS`.

**`add_dependency()`** — accept optional `relationship_type` parameter (default `'blocks'`):
```python
def add_dependency(conn, task_id, depends_on_id, relationship_type="blocks"):
    # ... existing validation ...
    conn.execute(
        "INSERT INTO task_dependencies (task_id, depends_on_id, relationship_type) VALUES (?, ?, ?)",
        (task_id, depends_on_id, relationship_type)
    )
```

**`add` CLI subcommand** — add optional `--type` flag:
```
python3 scripts/manage_dependencies.py add 10 5 --type contingent
```

**Display commands** (`list_dependencies`, `list_dependents`, `show_all`) — include `relationship_type` in output columns so the type is visible.

### 3. `/groom-backlog` — New Step 0c: Cascade-Close Moot Contingent Tasks

After Step 0b (flag merged PRs), before Step 1:

```bash
# Find contingent dependents of tasks closed as wont_do/expired
tusk -header -column "
SELECT t.id, t.summary, t.status,
       d.depends_on_id as closed_task_id,
       upstream.summary as closed_task_summary,
       upstream.closed_reason
FROM tasks t
JOIN task_dependencies d ON t.id = d.task_id
JOIN tasks upstream ON d.depends_on_id = upstream.id
WHERE t.status <> 'Done'
  AND d.relationship_type = 'contingent'
  AND upstream.status = 'Done'
  AND upstream.closed_reason IN ('wont_do', 'expired')
"
```

For each match, auto-close:
```bash
tusk "UPDATE tasks SET status = 'Done', closed_reason = 'wont_do',
  updated_at = datetime('now'),
  description = description || char(10) || char(10) || '---' || char(10) ||
    'Auto-closed: Contingent on TASK-<upstream_id> which closed as <reason> (' || datetime('now') || ').'
WHERE id = <id>"
```

### 4. `/groom-backlog` — Priority Scoring Tweak (Step 7)

Optional enhancement: discount `priority_score` for tasks whose blockers are all contingent (uncertain value). Could subtract 5–10 points. This biases `/next-task` toward tasks with guaranteed value.

```sql
-- In the priority_score UPDATE, add a contingent penalty:
- CASE WHEN EXISTS (
    SELECT 1 FROM task_dependencies d
    WHERE d.task_id = tasks.id AND d.relationship_type = 'contingent'
  ) AND NOT EXISTS (
    SELECT 1 FROM task_dependencies d
    WHERE d.task_id = tasks.id AND d.relationship_type = 'blocks'
  ) THEN -10 ELSE 0 END
```

This is optional and can be deferred — the cascade-close behavior is the primary value.

### 5. `/manage-dependencies` Skill Update

Document the `--type` flag:
```
python3 scripts/manage_dependencies.py add 10 5 --type contingent
```

### 6. No Changes Required

These queries/files need **no modifications** — they use `NOT EXISTS` patterns that treat all dependency types as blocking, which is correct for both `blocks` and `contingent`:

- `/next-task` — all task selection queries (5 queries)
- `/next-task` — newly unblocked tasks query
- `manage_dependencies.py` — `would_create_cycle()` DFS
- `manage_dependencies.py` — `show_blocked()`, `show_ready()`

### Files Changed Summary

| File | Change | Scope |
|------|--------|-------|
| `bin/tusk` | Migration 2→3, init schema update | ~15 lines |
| `scripts/manage_dependencies.py` | Add `--type` param, display type in output | ~20 lines |
| `skills/groom-backlog/SKILL.md` | New Step 0c (cascade-close) | ~25 lines |
| `skills/manage-dependencies/SKILL.md` | Document `--type` flag | ~5 lines |

### What Does NOT Change

- All `/next-task` queries (blocking semantics identical for both types)
- `would_create_cycle()` DFS (cycles are cycles regardless of type)
- `show_blocked()` / `show_ready()` (blocked is blocked regardless of type)
- No other skills affected (`/create-task`, `/check-dupes`, `/retro`)
- No table recreation needed
- No trigger changes needed
