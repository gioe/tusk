# tusk

A portable task management system for [Claude Code](https://claude.ai/claude-code) projects. Gives Claude a local SQLite database, CLI, and skills to track, prioritize, and work through tasks autonomously.

## What You Get

- **`tusk` CLI** — single entry point for all task database operations
- **Skills** — Claude Code skills for task workflows (`/tusk-init`, `/next-task`, `/groom-backlog`, `/check-dupes`, `/manage-dependencies`, `/tasks`)
- **Scripts** — Python utilities for duplicate detection and dependency management
- **Config-driven schema** — define your project's domains, task types, and agents in JSON; validation triggers are generated automatically

## Quick Start

```bash
# Clone the repo somewhere on your machine
git clone https://github.com/gioe/tusker.git

# From your project root (must be a git repo)
cd /path/to/your/project
/path/to/tusker/install.sh
```

This will:
1. Install `tusk`, skills, scripts, and default config
2. Create `tusk/config.json` with defaults
3. Initialize the database at `tusk/tasks.db`

Then start a new Claude Code session and run `/tusk-init` — it will scan your codebase, suggest domains and agents, write your config, and seed tasks from TODOs.

You can also configure manually by editing `tusk/config.json` and running `tusk init --force`.

### Upgrading

To pull the latest version of tusk into an installed project:

```bash
tusk upgrade
```

This downloads the latest release from GitHub, updates all files (CLI, skills, scripts), and runs schema migrations. Your config (`tusk/config.json`) and database (`tusk/tasks.db`) are never touched.

## Configuration

Edit `tusk/config.json` after install:

```json
{
  "domains": ["Frontend", "Backend", "Infrastructure", "Docs"],
  "task_types": ["bug", "feature", "refactor", "test", "docs", "infrastructure"],
  "statuses": ["To Do", "In Progress", "Done"],
  "priorities": ["Highest", "High", "Medium", "Low", "Lowest"],
  "closed_reasons": ["completed", "expired", "wont_do", "duplicate"],
  "agents": {
    "frontend-engineer": "React, CSS, and UI components",
    "backend-engineer": "API endpoints, database, and server logic"
  }
}
```

- **domains**: Empty array means no domain validation (any value accepted)
- **task_types**: Empty array means no task_type validation
- **agents**: Used by `/groom-backlog` to auto-assign tasks; empty object skips assignment
- **statuses**, **priorities**, **closed_reasons**: Changing these is possible but not recommended

## CLI Reference

```bash
tusk "SELECT ..."           # Run SQL
tusk -header -column "SQL"   # With formatting flags
tusk path                    # Print resolved DB path
tusk config                  # Print full config JSON
tusk config domains          # List valid domains
tusk config agents           # List configured agents
tusk init                    # Bootstrap DB (safe — skips if exists)
tusk init --force            # Recreate DB from scratch
tusk shell                   # Interactive sqlite3 shell
tusk version                 # Print installed version
tusk migrate                 # Apply pending schema migrations
tusk upgrade                 # Upgrade tusk from GitHub
```

## Skills

| Skill | Description |
|-------|-------------|
| `/next-task` | Get the highest-priority ready task and start working on it |
| `/next-task 42` | Begin the full dev workflow on task #42 |
| `/next-task list 5` | Show top 5 ready tasks |
| `/next-task preview` | Show next task without starting it |
| `/groom-backlog` | Analyze and clean up the backlog |
| `/check-dupes` | Check for duplicate tasks before creating new ones |
| `/manage-dependencies` | Add, remove, or query task dependencies |
| `/tasks` | Open DB Browser for SQLite |
| `/tusk-init` | Interactive setup wizard — scans codebase, suggests config, seeds tasks |

## CLAUDE.md Setup

The `/tusk-init` skill can generate this automatically. To add it manually:

```markdown
## Task Queue

The project task database is managed via `tusk`. Use it for all task operations:

    tusk "SELECT ..."          # Run SQL
    tusk -header -column "SQL"  # With formatting flags
    tusk path                   # Print resolved DB path
    tusk config                 # Print project config
    tusk init                   # Bootstrap DB

Never hardcode the DB path — always go through `tusk`.
```

## Schema

The database has three tables:

### tasks
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Auto-incrementing primary key |
| summary | TEXT | Brief task title (required) |
| description | TEXT | Detailed description, acceptance criteria |
| status | TEXT | `To Do`, `In Progress`, `Done` |
| priority | TEXT | `Highest`, `High`, `Medium`, `Low`, `Lowest` |
| domain | TEXT | Project area (validated if configured) |
| assignee | TEXT | Agent name |
| task_type | TEXT | `bug`, `feature`, `refactor`, etc. |
| priority_score | INTEGER | Pre-computed score for task selection |
| github_pr | TEXT | PR URL when work is in progress |
| expires_at | TEXT | Auto-close date for deferred tasks |
| closed_reason | TEXT | `completed`, `expired`, `wont_do`, `duplicate` |
| created_at | TEXT | Creation timestamp |
| updated_at | TEXT | Last update timestamp |

### task_dependencies
Tracks which tasks block other tasks. Enforces no self-dependencies and no circular dependencies (via the Python script).

### task_sessions
Optional metrics tracking for time, cost, and token usage per task.

## Pricing

`pricing.json` contains per-model token rates (USD per million tokens) used by `tusk session-stats` to compute the `cost_dollars` column in `task_sessions`. It ships with tusk and is updated via `tusk pricing-update`.

### Structure

```json
{
  "models": {
    "claude-sonnet-4-6": {
      "input": 3.0,
      "cache_write_5m": 3.75,
      "cache_write_1h": 6.0,
      "cache_read": 0.3,
      "output": 15.0
    }
  },
  "aliases": {
    "claude-sonnet-4-6-20250918": "claude-sonnet-4-6"
  }
}
```

- **`models`**: Canonical model IDs mapped to USD per million tokens (e.g., `"input": 3.0` = $3.00/MTok) for five token categories
- **`aliases`**: Date-stamped model IDs mapped to their canonical key (e.g., `claude-sonnet-4-6-20250918` → `claude-sonnet-4-6`)

### How costs are calculated

`tusk-session-stats.py` parses Claude Code JSONL transcripts, aggregates the `usage` object from each API response, resolves the model ID (exact match → alias lookup → prefix match), and computes cost as:

```
cost = (usage.input_tokens / 1M × input)
     + (cache_creation.ephemeral_5m_input_tokens / 1M × cache_write_5m)
     + (cache_creation.ephemeral_1h_input_tokens / 1M × cache_write_1h)
     + (usage.cache_read_input_tokens / 1M × cache_read)
     + (usage.output_tokens / 1M × output)
```

The left side of each term comes from the transcript; the right side comes from the model's entry in `pricing.json`. When the nested `cache_creation` object is absent (older transcripts), all `cache_creation_input_tokens` are assigned to the 5m tier as a fallback. Claude Code automatically writes JSONL transcripts to `~/.claude/projects/<project_hash>/` during each session — tusk reads these but never writes them. A typical usage object in the transcript looks like:

```json
{
  "input_tokens": 2750,
  "output_tokens": 483,
  "cache_creation_input_tokens": 12500,
  "cache_read_input_tokens": 8200,
  "cache_creation": {
    "ephemeral_5m_input_tokens": 10000,
    "ephemeral_1h_input_tokens": 2500
  }
}
```

If `pricing.json` is missing or a model isn't found, cost defaults to `$0` with a warning.

### Updating prices

```bash
tusk pricing-update              # Fetch latest from Anthropic and update (both cache tiers)
tusk pricing-update --dry-run    # Show diff without writing
tusk session-recalc              # Re-run cost calculations for all existing sessions
```

## How It Works

The `tusk` CLI is the single source of truth for the database path. Everything references it:

- **Skills** call `tusk "SQL"` (never raw `sqlite3`)
- **Python scripts** resolve the path via `subprocess.check_output(["tusk", "path"])`
- **Config** lives at `tusk/config.json`; triggers are generated from it at init time

If the DB path ever changes, update one line in `bin/tusk`.

## File Structure

After installation, your project will have:

```
your-project/
├── .claude/
│   ├── bin/
│   │   ├── tusk                       # CLI (single source of truth)
│   │   ├── tusk-dupes.py              # Duplicate detection (via tusk dupes)
│   │   ├── tusk-session-stats.py      # Token/cost tracking (via tusk session-stats)
│   │   ├── config.default.json        # Fallback config
│   │   ├── pricing.json               # Per-model token rates (USD/MTok)
│   │   └── VERSION                    # Installed distribution version
│   └── skills/
│       ├── next-task/SKILL.md
│       ├── groom-backlog/SKILL.md
│       ├── check-dupes/SKILL.md
│       ├── manage-dependencies/SKILL.md
│       ├── tasks/SKILL.md
│       └── tusk-init/SKILL.md
├── scripts/
│   └── manage_dependencies.py
└── tusk/
    ├── config.json                    # Your project's config
    └── tasks.db                       # The database
```
