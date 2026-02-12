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
python3 scripts/manage_dependencies.py add <task_id> <depends_on_id>
```

Example: Task 5 cannot start until Task 3 is done:
```bash
python3 scripts/manage_dependencies.py add 5 3
```

### Remove a dependency

```bash
python3 scripts/manage_dependencies.py remove <task_id> <depends_on_id>
```

### List dependencies for a task

Show all tasks that must be completed before a specific task can start:

```bash
python3 scripts/manage_dependencies.py list <task_id>
```

### List dependents of a task

Show all tasks that are waiting on a specific task:

```bash
python3 scripts/manage_dependencies.py dependents <task_id>
```

### Show blocked tasks

```bash
python3 scripts/manage_dependencies.py blocked
```

### Show ready tasks

```bash
python3 scripts/manage_dependencies.py ready
```

### Show all dependencies

```bash
python3 scripts/manage_dependencies.py all
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
