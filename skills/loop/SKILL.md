---
name: loop
description: Autonomously work through the backlog — dispatches /chain for chain heads, /next-task for standalone tasks, repeating until empty
allowed-tools: Bash
---

# Loop

Runs the autonomous backlog loop via the `tusk loop` CLI command. Queries the highest-priority ready task, dispatches it to `/chain` (if it has dependents) or `/next-task` (standalone), and repeats until the backlog is empty or a stop condition is met.

## Usage

```bash
# Run until backlog is empty
tusk loop

# Stop after N tasks
tusk loop --max-tasks N

# Preview what would run without executing
tusk loop --dry-run
```

## Behavior

1. Queries the highest-priority unblocked task (same ranking as `/next-task`)
2. Checks whether the task is a chain head (has non-Done downstream dependents via `tusk chain scope`)
3. Dispatches:
   - **Chain head** → `claude -p /chain <id>`
   - **Standalone** → `claude -p /next-task <id>`
4. Stops on non-zero exit from any agent, on empty backlog, or when `--max-tasks` is reached

## Flags

| Flag | Description |
|------|-------------|
| `--max-tasks N` | Stop after N tasks (default: unlimited) |
| `--dry-run` | Print what would run without executing |
