---
name: loop
description: Autonomously work through the backlog — dispatches /chain for chain heads, /tusk for standalone tasks, repeating until empty
allowed-tools: Bash
---

# Loop

Runs the autonomous backlog loop via the `tusk loop` CLI command. Queries the highest-priority ready task, dispatches it to `/chain` (if it has dependents) or `/tusk` (standalone), and repeats until the backlog is empty or a stop condition is met.

> **Prefer `/create-task` for all task creation.** It handles decomposition, deduplication, acceptance criteria generation, and dependency proposals in one workflow. Use `bin/tusk task-insert` directly only when scripting bulk inserts or in automated contexts where the interactive review step is not applicable.

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

1. Queries the highest-priority unblocked task (same ranking as `/tusk`)
2. Checks whether the task is a chain head (has non-Done downstream dependents via `tusk chain scope`)
3. Dispatches:
   - **Chain head** → `claude -p /chain <id>`
   - **Standalone** → `claude -p /tusk <id>`
4. Stops on non-zero exit from any agent, on empty backlog, or when `--max-tasks` is reached

> **Note:** Tasks dispatched via `/tusk` or `/chain` use `tusk task-start --force` so that zero-criteria tasks emit a warning rather than hard-failing the automated workflow.

## Flags

| Flag | Description |
|------|-------------|
| `--max-tasks N` | Stop after N tasks (default: unlimited) |
| `--dry-run` | Print what would run without executing |
