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

# Unattended run — skip stuck chain tasks and continue
tusk loop --on-failure skip

# Unattended run — abort the chain on first stuck task
tusk loop --on-failure abort
```

## Behavior

1. Selects the next task via `tusk task-select` (same ranking as `/tusk`)
2. Checks whether the task is a chain head (has non-Done downstream dependents via `tusk chain scope`)
3. Dispatches:
   - **Chain head** → `claude -p /chain <id> [--on-failure <strategy>]`
   - **Standalone** → `claude -p /tusk <id>`
4. Stops on non-zero exit from any agent, on empty backlog, or when `--max-tasks` is reached

> **Note:** Tasks dispatched via `/tusk` or `/chain` use `tusk task-start --force` so that zero-criteria tasks emit a warning rather than hard-failing the automated workflow.

## Flags

| Flag | Description |
|------|-------------|
| `--max-tasks N` | Stop after N tasks (default: unlimited) |
| `--dry-run` | Print what would run without executing |
| `--on-failure skip\|abort` | Unattended failure strategy passed through to each `/chain` dispatch. **skip** — log a warning for each stuck task and continue to the next wave. **abort** — stop the chain immediately and report all incomplete tasks. Has no effect on standalone `/tusk` dispatches. Omit for interactive mode (default). |

## Headless / CI Usage

`/loop` can be run unattended via `claude -p` (non-interactive print mode):

```bash
# Run up to 5 tasks; abort if any chain task gets stuck
claude -p /loop --max-tasks 5 --on-failure abort

# Run until empty; skip stuck chain tasks and continue
claude -p /loop --on-failure skip
```

**Prerequisites for unattended runs:**
- `--on-failure` is **required** for unattended operation. Without it, a stuck chain sub-agent exits its session in headless mode, causing the loop to stop at that task with a non-zero exit code — leaving the remaining backlog unprocessed.
- `--max-tasks N` is recommended in CI to cap unbounded execution and avoid runaway costs.

**When to use each strategy:**
- `--on-failure abort` — prefer this in CI pipelines where a stuck task signals a problem that needs human attention. The loop stops and reports incomplete tasks; whether the CI job fails depends on how the Claude CLI exits.
- `--on-failure skip` — prefer this for overnight batch runs where partial progress is acceptable and you want to drain as much of the backlog as possible.
