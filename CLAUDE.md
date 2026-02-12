# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

claude-taskdb is a portable task management system for Claude Code projects. It provides a local SQLite database, a bash CLI (`bin/taskdb`), Python utility scripts, and Claude Code skills to track, prioritize, and work through tasks autonomously.

## Commands

```bash
# Initialize (or recreate) the database
bin/taskdb init [--force]

# Run SQL against the task database
bin/taskdb "SELECT * FROM tasks WHERE status = 'To Do'"
bin/taskdb -header -column "SELECT id, summary, status FROM tasks"

# Print resolved DB path
bin/taskdb path

# Print config (full or by key)
bin/taskdb config
bin/taskdb config domains

# Interactive sqlite3 shell
bin/taskdb shell
```

There is no build step, test suite, or linter in this repository.

## Architecture

### Single Source of Truth: `bin/taskdb`

The bash CLI resolves all paths dynamically. The database lives at `<repo_root>/taskdb/tasks.db`. Everything references `bin/taskdb` — skills call it for SQL, Python scripts call `subprocess.check_output([".claude/bin/taskdb", "path"])` to resolve the DB path. Never hardcode the database path.

### Config-Driven Validation

`config.default.json` defines domains, task_types, statuses, priorities, closed_reasons, and agents. On `taskdb init`, SQLite validation triggers are **auto-generated** from the config via an embedded Python snippet in `bin/taskdb`. Empty arrays (e.g., `"domains": []`) disable validation for that column.

### Skills (installed to `.claude/skills/` in target projects)

- **`/next-task`** — Selects the highest-priority unblocked task and begins a full dev workflow (branching, implementation, PR)
- **`/groom-backlog`** — Auto-closes expired deferred tasks, scans for duplicates, categorizes and re-prioritizes the backlog
- **`/check-dupes`** — Similarity-based duplicate detection (uses `difflib.SequenceMatcher`, thresholds 0.60–0.82)
- **`/manage-dependencies`** — Add/remove/query task dependencies with circular dependency prevention (DFS)
- **`/tasks`** — Opens the database in DB Browser for SQLite

### Python Scripts (`scripts/`)

- `check_duplicates.py` — Duplicate detection against open tasks. Normalizes summaries by stripping `[Deferred]`, `[Enhancement]`, etc. prefixes.
- `manage_dependencies.py` — Dependency graph management. Validates no self-deps and no cycles before inserting.

Both scripts resolve the DB path at runtime via `bin/taskdb path`.

### Database Schema

Three tables: `tasks` (13 columns — summary, status, priority, domain, assignee, task_type, priority_score, etc.), `task_dependencies` (composite PK with cascade deletes + no-self-dep CHECK), `task_sessions` (optional metrics). One view: `task_metrics` (aggregates sessions per task).

### Installation Model

`install.sh` copies `bin/taskdb` → `.claude/bin/taskdb`, skills → `.claude/skills/`, scripts → `scripts/`, and runs `taskdb init`. This repo is the source; target projects get the installed copy.

## Key Conventions

- All DB access goes through `bin/taskdb`, never raw `sqlite3`
- Task workflow: `To Do` → `In Progress` → `Done` (must set `closed_reason` when marking Done)
- Priority scoring: `base_priority + source_bonus + unblocks_bonus`
- Deferred tasks from PR reviews get `[Deferred]` prefix and 60-day `expires_at`
- Always run `/check-dupes` before inserting new tasks
- Dependencies use DFS cycle detection in Python; SQLite CHECK prevents self-loops
