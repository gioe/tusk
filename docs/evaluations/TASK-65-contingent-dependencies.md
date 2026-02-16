# TASK-65: Evaluation — Contingent/Conditional Dependencies

## Problem Statement

The current `task_dependencies` table models only strict "A blocks B" gates. There is no way to express conditional relationships where the outcome of Task A determines whether Task B is needed at all.

**Real example:** "Evaluate whether heuristic dupe checker is needed" (TASK-64) may make "Remove redundant Step 3b from /retro" either necessary or obsolete depending on findings. Today you must either insert both and risk clutter, or hold one in someone's head.

## Current State

- `task_dependencies` schema: `(task_id, depends_on_id, created_at)` — simple blocking edges
- 16 SQL queries across the codebase reference this table
- Cycle detection via DFS in Python (`manage_dependencies.py`)
- Self-loop prevention via CHECK constraint
- Touch points: `bin/tusk`, `scripts/manage_dependencies.py`, `/next-task`, `/groom-backlog`, `/manage-dependencies`

## Approaches Evaluated

### Option A: Dependency Type Column

Add a `relationship_type TEXT DEFAULT 'blocks'` column to `task_dependencies`.

Values: `blocks` (current behavior), `contingent` (outcome-dependent), `informs` (advisory).

**Pros:**
- Minimal schema change — one column, backward-compatible default
- Simple migration (`ALTER TABLE ADD COLUMN` — no table recreation needed)
- Existing queries unchanged if they don't filter by type (current `NOT EXISTS` checks would still treat all deps as blocking unless explicitly filtered)
- `manage_dependencies.py` just needs an optional `--type` parameter

**Cons:**
- Every dependency query must decide whether to filter by type — if queries aren't updated, contingent deps silently act as hard blocks (defeating the purpose)
- Semantic ambiguity: what does `/next-task` do with a contingent dependency? Skip it? Show a warning? The blocking semantics that make dependencies useful break down for non-blocking types
- Invites complexity creep — once you have types, requests for more types follow
- Net effect is essentially metadata (a structured comment saying "related") unless significant query logic changes accompany it

**Migration complexity:** Low
**Behavior change complexity:** Medium-High (queries need updating to be useful)
**Files affected:** 6 files, ~16 queries to audit

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
- Tusk's core strength is simplicity — this preserves it
- LLM agents reading task descriptions can reason about natural-language contingencies ("This task is contingent on TASK-64 — only proceed if the heuristic checker is kept")
- `/groom-backlog` already reviews task descriptions and can close obsoleted tasks during regular grooming
- The actual frequency of contingent relationships is low — the TASK-64 example is the only concrete case cited

**Cons:**
- No machine-readable signal — can't auto-close contingent tasks when a parent's outcome makes them moot
- Relies on LLM judgment during grooming to notice and act on textual contingencies
- Risk of stale tasks that should have been obsoleted but weren't caught

**Migration complexity:** None
**Behavior change complexity:** None
**Files affected:** 0

### Option D: Status-Based Branching

Extend `closed_reason` or add an `outcome` field so downstream tasks can be auto-closed when a parent's outcome makes them moot.

**Pros:**
- Could enable automatic cascade closure of contingent work

**Cons:**
- `closed_reason` describes why a task closed (`completed`, `wont_do`), not what the task's output was — conflating these concepts muddies both
- Requires a new concept of "task outcome" distinct from closure reason
- No obvious mapping: how does a downstream task declare "close me if TASK-X outcome is Y"? This needs a rule engine
- Highest conceptual complexity of all options for the least clear benefit

**Migration complexity:** Medium
**Behavior change complexity:** Very High
**Files affected:** 8+ files, new triggering infrastructure

## Recommendation: Option C — Notes/Metadata Only

**Keep the current model and document contingencies in task descriptions.**

### Rationale

1. **Simplicity is tusk's moat.** The flat task list with simple blocking edges is easy to reason about — for both LLMs and humans. Every schema addition is a tax on every future skill and query.

2. **The problem is rare.** The evaluation surfaced exactly one concrete example (TASK-64). Building schema infrastructure for an edge case violates YAGNI.

3. **LLM agents handle ambiguity well.** Unlike traditional project management tools that need machine-readable fields, tusk's users are Claude Code agents. An agent running `/groom-backlog` that reads "contingent on TASK-64 outcome" in a description can decide to close the task — no schema support needed.

4. **Existing tools already cover the workflow.** `/groom-backlog` reviews open tasks and closes stale/obsoleted ones. Writing "Contingent on TASK-X: proceed only if [condition]" in the description is sufficient signal for grooming to act on.

5. **Options A and D sound simple but aren't.** A type column without query updates is dead metadata. With query updates, you're changing the semantics of 16 queries and 6 files — a significant maintenance surface for a rare need. Option D requires a rule engine that doesn't exist.

### Suggested Convention

When creating a task that depends on another task's outcome (not just its completion), include a structured note in the description:

```
Contingent on TASK-<id>: <condition under which this task should proceed>
If TASK-<id> outcome makes this moot, close as wont_do.
```

This gives `/groom-backlog` a grep-able pattern to check during backlog review without any schema changes.

### When to Revisit

Revisit this decision if:
- Contingent relationships become frequent (5+ concurrent instances)
- Tasks are regularly missed during grooming because contingencies weren't noticed
- A new tusk capability (e.g., automated task lifecycle) needs machine-readable relationship types
