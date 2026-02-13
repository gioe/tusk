# tusker

A portable task management system for [Claude Code](https://claude.ai/claude-code) projects. Gives Claude a local SQLite database, CLI, and skills to track, prioritize, and work through tasks autonomously.

## What You Get

- **`tusk` CLI** — single entry point for all task database operations
- **Skills** — Claude Code skills for task workflows (`/next-task`, `/groom-backlog`, `/check-dupes`, `/manage-dependencies`, `/tasks`)
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

Then edit `tusk/config.json` to set your project's domains and agents, and re-init:

```bash
tusk init --force
```

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
    "frontend-engineer": { "domains": ["Frontend"], "keywords": ["React", "CSS", "component"] },
    "backend-engineer": { "domains": ["Backend"], "keywords": ["API", "endpoint", "database"] }
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

## CLAUDE.md Setup

Add this to your project's `CLAUDE.md`:

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
│   │   └── config.default.json        # Fallback config
│   └── skills/
│       ├── next-task/SKILL.md
│       ├── groom-backlog/SKILL.md
│       ├── check-dupes/SKILL.md
│       ├── manage-dependencies/SKILL.md
│       └── tasks/SKILL.md
├── scripts/
│   ├── check_duplicates.py
│   └── manage_dependencies.py
└── tusk/
    ├── config.json                    # Your project's config
    └── tasks.db                       # The database
```
