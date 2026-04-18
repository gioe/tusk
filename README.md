# tusk

A portable task management system for [Claude Code](https://claude.ai/claude-code) projects. Gives Claude a local SQLite database, CLI, and skills to track, prioritize, and work through tasks autonomously.

## What You Get

- **`tusk` CLI** — single entry point for all task database operations
- **Skills** — Claude Code skills for task workflows (`/tusk-init`, `/tusk`, `/groom-backlog`)
- **Scripts** — Python utilities for duplicate detection and dependency management
- **Config-driven schema** — define your project's domains, task types, and agents in JSON; validation triggers are generated automatically

## Quick Start

```bash
# Clone the repo somewhere on your machine
git clone https://github.com/gioe/tusk.git

# From your project root (must be a git repo)
cd /path/to/your/project
/path/to/tusk/install.sh
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
  },
  "test_command": "pytest tests/",  // shell command run by `tusk commit` before staging; empty string disables
  "review": {
    "mode": "ai_only",              // "disabled" skips /review-commits; "ai_only" runs one AI reviewer
    "max_passes": 2,                // maximum review-fix-re-review cycles before stopping
    "reviewer": { "name": "general", "description": "..." }  // single AI reviewer persona; omit for inline review only
  },
  "dupes": {
    "check_threshold": 0.82,        // cosine similarity above which a task is flagged as a likely duplicate
    "similar_threshold": 0.6        // cosine similarity above which a task is surfaced as possibly similar
  },
  "merge": {
    "mode": "local"                 // "local" = fast-forward merge; "pr" = squash-merge via gh pr merge
  },
  "project_type": "ios_app"        // selects bootstrap/<project_type>.json for task seeding (null = disabled)
}
```

- **domains**: Empty array means no domain validation (any value accepted)
- **task_types**: Empty array means no task_type validation
- **agents**: Used by `/groom-backlog` to auto-assign tasks; empty object skips assignment
- **statuses**, **priorities**, **closed_reasons**: Changing these is possible but not recommended
- **test_command**: Shell command run by `tusk commit` before staging files; a non-zero exit blocks the commit. Empty string disables the check.
- **review.mode**: Controls `/review-commits` behavior — `"disabled"` skips AI review entirely; `"ai_only"` runs the configured reviewer
- **review.max_passes**: Maximum number of review → fix → re-review cycles before the skill stops iterating
- **review.reviewer**: Optional AI reviewer persona with `name` and `description`. When absent, `/review-commits` falls back to inline (non-agent) review
- **dupes.check_threshold**: Cosine similarity score (0–1) above which a candidate is treated as a likely duplicate and blocked
- **dupes.similar_threshold**: Cosine similarity score (0–1) above which a candidate is surfaced as possibly similar (for human review)
- **merge.mode**: `"local"` performs a fast-forward merge directly; `"pr"` squash-merges via `gh pr merge`
- **project_type**: Selects which `bootstrap/<project_type>.json` file is used for task seeding during `/tusk-init`; `null` disables seeding

### Project Bootstrap

During `/tusk-init`, tusk can seed your backlog with a starter set of tasks based on your project type. Set `project_type` in `tusk/config.json` (or via `/tusk-update`) to enable this.

Two built-in project types are included:

| `project_type` | Library repo | What gets seeded |
|---|---|---|
| `ios_app` | [gioe/ios-libs](https://github.com/gioe/ios-libs) — standalone Swift Package providing SharedKit (UI design tokens) and APIClient (HTTP client) | Tasks for adding the SPM dependency, configuring design tokens, and wiring up APIClient |
| `python_service` | [gioe/python-libs](https://github.com/gioe/python-libs) — standalone Python package (`gioe-libs`) providing structured logging and observability utilities | Tasks for installing the package, configuring structured logging, and enabling observability |

Bootstrap files live under `bootstrap/` in the tusk source repo and are copied to `.claude/bin/bootstrap/` at install time. A new reader can review them to see exactly which tasks they will receive before running `/tusk-init`.

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
| `/tusk` | Get the highest-priority ready task and start working on it |
| `/tusk 42` | Begin the full dev workflow on task #42 |
| `/tusk list 5` | Show top 5 ready tasks |
| `/tusk preview` | Show next task without starting it |
| `/groom-backlog` | Analyze and clean up the backlog |
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
│       ├── tusk/SKILL.md
│       ├── groom-backlog/SKILL.md
│       ├── tasks/SKILL.md
│       └── tusk-init/SKILL.md
├── scripts/
│   └── manage_dependencies.py
└── tusk/
    ├── config.json                    # Your project's config
    └── tasks.db                       # The database
```

## Troubleshooting

### Skill not found after install

If Claude Code reports an unknown skill (e.g., `/tusk` not recognized) immediately after running `install.sh`, the skill was installed mid-session and has not been discovered yet.

**Resolution:** Start a new Claude Code session. Skills are discovered at session startup — a skill added after the session began will not be available until you restart.

### Task stuck in the wrong state

If a task is stuck `In Progress` when it should be `To Do`, or you need to force-close a task that has open criteria:

```bash
# Reopen an In Progress task back to To Do
tusk task-reopen <task_id> --force

# Force-close a task (e.g., wont_do) even if criteria are open
tusk task-done <task_id> --reason wont_do --force
```

### Migration failure

If `tusk migrate` fails or reports that schema changes cannot be applied, check for a version mismatch between the installed CLI and the database schema:

```bash
tusk version          # distribution version of the installed CLI
tusk shell            # opens sqlite3 shell; then run: PRAGMA user_version;
```

If `user_version` is ahead of what the installed CLI knows about, you may have downgraded the CLI. Re-run `tusk upgrade` to restore the latest version, then retry `tusk migrate`.

### Database corruption

If the database is corrupted and `tusk` commands are failing with SQLite errors:

> **Warning:** `tusk init --force` **destroys all existing task data**. Back up `tusk/tasks.db` before proceeding.

```bash
cp tusk/tasks.db tusk/tasks.db.bak   # back up first
tusk init --force                     # recreate the database from scratch
```

After reinitializing, re-run `tusk migrate` to apply any pending schema migrations.

## Reporting Issues

Found a bug or have a feature request? Open an issue at https://github.com/gioe/tusk/issues.

If you're reporting a problem from a project where tusk is installed (rather than from the tusk source repo itself), please use the **[Tusk instance feedback](https://github.com/gioe/tusk/issues/new?template=tusk-instance-feedback.md)** issue template. It prompts for your tusk version, project context, observed behavior, reproduction steps, and expected behavior — which helps diagnose issues across different installation environments.

> **Note:** Do not patch `.claude/bin/` files directly. Those files are managed by tusk and will be overwritten the next time you run `tusk upgrade`. To contribute a fix, open an issue or submit a pull request to the source repository.
