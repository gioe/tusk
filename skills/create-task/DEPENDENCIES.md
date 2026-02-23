# Dependency Proposals

After inserting tasks, analyze them for dependencies — both among the newly created tasks **and** against existing open backlog tasks (fetched in Step 2b).

## How to identify dependencies among new tasks

Look for these patterns among the newly created tasks:

- **Schema before code** — a migration or data model task should block feature tasks that depend on it
- **Backend before frontend** — API endpoints often block UI tasks that consume them
- **Core before extension** — foundational tasks (setup, config, base classes) block tasks that build on them
- **Implementation before docs** — documentation tasks should depend on the feature they document
- **Bug fix before feature** — if a new feature depends on a bug being fixed first
- **Contingent relationships** — if task B only makes sense when task A is completed successfully (not just closed), use `contingent` type. Example: "Write integration tests for new API" is contingent on "Build new API endpoint" — if the endpoint is cancelled, the tests are moot

## How to identify dependencies on existing backlog tasks

Cross-reference each newly created task against the existing open tasks from Step 2b. Look for:

- **New task extends existing work** — a new feature that builds on top of functionality described by an existing task (e.g., new "Add admin dashboard" depends on existing "Implement role-based auth")
- **New task consumes existing deliverable** — a new frontend task that needs an API endpoint tracked in an existing backlog task
- **New task requires existing fix** — a new feature that won't work correctly until an existing bug-fix task is completed
- **Shared subsystem** — if an existing task modifies the same subsystem in a way that would conflict or needs to land first

**Important**: Only propose a dependency when there is a clear, concrete reason one task must complete before the other can begin. Do not propose dependencies based on vague thematic similarity — tasks in the same domain are not automatically dependent.

If no natural ordering exists (among new tasks or against the backlog), state that and skip dependency insertion.

## Present proposed dependencies

Show a numbered table of proposed dependencies for user approval. Mark whether each dependency is between two new tasks or between a new task and an existing backlog task:

```markdown
## Proposed Dependencies

| # | Task | Depends On | Type | Reason |
|---|------|------------|------|--------|
| 1 | #16 Add signup page (new) | #14 Add auth endpoint (new) | blocks | Frontend consumes the auth API |
| 2 | #17 Write API docs (new) | #14 Add auth endpoint (new) | contingent | Docs are moot if endpoint is cancelled |
```

Then ask:

> Does this look right? You can:
> - **Confirm** to add all dependencies
> - **Remove** specific numbers (e.g., "remove 2")
> - **Change type** (e.g., "change 1 to contingent")
> - **Skip** to add no dependencies

Wait for explicit user approval before inserting.

## Insert approved dependencies

For each approved dependency, run:

```bash
tusk deps add <task_id> <depends_on_id> [--type contingent]
```

Report any validation errors (cycle detected, task not found) and continue with the remaining dependencies.
