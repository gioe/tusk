# Loop — Autonomous Backlog Loop (Codex)

Runs the autonomous backlog loop via the `tusk loop` CLI. Queries the
highest-priority ready task, dispatches it to the chain or task workflow,
and repeats until the backlog is empty or a stop condition is met.

> **Conventions:** Run `tusk conventions search <topic>` for project rules.
> Do not restate convention text inline — it drifts from the DB.

> **Sequential execution — no parallel sub-agents.** Codex has no Task tool
> for spawning background agents, so `/loop` runs **one task at a time**.
> Chain heads still drain their downstream sub-DAG via `tusk chain frontier`,
> but every wave is processed sequentially in the current Codex session
> (see `chain.md`). Do not attempt to launch parallel Codex sessions from
> within this prompt.

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

1. Queries the highest-priority ready task (same WSJF ranking as
   `tusk task-select`).
2. Checks whether the task is a chain head (has non-Done downstream
   dependents via `tusk chain scope`).
3. Dispatches:
   - **Chain head** → follow `chain.md` for `<id>` (sequential wave loop).
   - **Standalone** → follow `tusk.md` for `<id>`.
4. Stops on non-zero exit from any dispatch, on empty backlog, or when
   `--max-tasks` is reached.

> **Note:** Tasks dispatched via `tusk.md` or `chain.md` use
> `tusk task-start --force` so that zero-criteria tasks emit a warning
> rather than hard-failing the automated workflow.

## Flags

| Flag | Description |
|------|-------------|
| `--max-tasks N` | Stop after N tasks (default: unlimited) |
| `--dry-run` | Print what would run without executing |
| `--on-failure skip\|abort` | Unattended failure strategy passed through to each chain dispatch. **skip** — log a warning for each stuck task and continue to the next wave. **abort** — stop the chain immediately and report all incomplete tasks. Has no effect on standalone task dispatches. Omit for interactive mode (default). |

## Headless / CI Usage

`/loop` can be run unattended via Codex's non-interactive print mode. When
using a CI-style runner, pass the loop arguments directly to the Codex CLI:

**Prerequisites for unattended runs:**
- `--on-failure` is **required** for unattended operation. Without it, a
  stuck chain task exits the session with a non-zero code, leaving the
  remaining backlog unprocessed.
- `--max-tasks N` is recommended in CI to cap unbounded execution and
  avoid runaway costs.

**When to use each strategy:**
- `--on-failure abort` — prefer in CI pipelines where a stuck task signals
  a problem that needs human attention. The loop stops and reports
  incomplete tasks.
- `--on-failure skip` — prefer for overnight batch runs where partial
  progress is acceptable and you want to drain as much of the backlog as
  possible.
