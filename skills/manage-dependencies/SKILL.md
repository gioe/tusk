---
name: manage-dependencies
description: Add, remove, or query task dependencies in the local tasks database
allowed-tools: Bash
---

# Manage Dependencies Skill

Manages task dependencies in the project task database (via `tusk` CLI). Dependencies define which tasks must be completed before another task can be started.

## Commands

### Add a dependency

Make a task depend on another task (the dependency must be completed first):

```bash
tusk deps add <task_id> <depends_on_id> [--type blocks|contingent]
```

The `--type` flag sets the relationship type (default: `blocks`):
- **`blocks`** — Standard blocking dependency. Downstream task becomes ready when upstream completes (any closed_reason).
- **`contingent`** — Outcome-dependent dependency. Both types block the downstream task from starting, but if the upstream task closes as `wont_do` or `expired`, the downstream task should be auto-closed as `wont_do` (the work is moot). `/groom-backlog` handles this automatically.

Examples:
```bash
# Task 5 cannot start until Task 3 is done (standard blocking)
tusk deps add 5 3

# Task 10 contingently depends on Task 5 (outcome-dependent)
tusk deps add 10 5 --type contingent
```

### Remove a dependency

```bash
tusk deps remove <task_id> <depends_on_id>
```

### List dependencies for a task

Show all tasks that must be completed before a specific task can start:

```bash
tusk deps list <task_id>
```

### List dependents of a task

Show all tasks that are waiting on a specific task:

```bash
tusk deps dependents <task_id>
```

### Show blocked tasks

```bash
tusk deps blocked
```

### Show ready tasks

```bash
tusk deps ready
```

### Show all dependencies

```bash
tusk deps all
```

## Validation

The script automatically validates:

- **Task existence**: Both tasks must exist in the database
- **Self-dependency**: A task cannot depend on itself
- **Circular dependencies**: Adding a dependency that would create a cycle is rejected

## Arguments

Parse the user's request to determine:
1. The command (add, remove, list, dependents, blocked, ready, all)
2. The task IDs involved (if applicable)

Then run the appropriate command from the examples above.
