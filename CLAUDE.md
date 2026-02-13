# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

tusker is a portable task management system for Claude Code projects. It provides a local SQLite database, a bash CLI (`bin/tusk`), Python utility scripts, and Claude Code skills to track, prioritize, and work through tasks autonomously.

## Commands

```bash
# Initialize (or recreate) the database
bin/tusk init [--force]

# Run SQL against the task database
bin/tusk "SELECT * FROM tasks WHERE status = 'To Do'"
bin/tusk -header -column "SELECT id, summary, status FROM tasks"

# Print resolved DB path
bin/tusk path

# Print config (full or by key)
bin/tusk config
bin/tusk config domains

# Interactive sqlite3 shell
bin/tusk shell
```

There is no build step, test suite, or linter in this repository.

## Architecture

### Single Source of Truth: `bin/tusk`

The bash CLI resolves all paths dynamically. The database lives at `<repo_root>/tusk/tasks.db`. Everything references `bin/tusk` — skills call it for SQL, Python scripts call `subprocess.check_output(["tusk", "path"])` to resolve the DB path. Never hardcode the database path.

### Config-Driven Validation

`config.default.json` defines domains, task_types, statuses, priorities, closed_reasons, and agents. On `tusk init`, SQLite validation triggers are **auto-generated** from the config via an embedded Python snippet in `bin/tusk`. Empty arrays (e.g., `"domains": []`) disable validation for that column.

### Skills (installed to `.claude/skills/` in target projects)

- **`/next-task`** — Selects the highest-priority unblocked task and begins a full dev workflow (branching, implementation, PR)
- **`/groom-backlog`** — Auto-closes expired deferred tasks, scans for duplicates, categorizes and re-prioritizes the backlog
- **`/check-dupes`** — Similarity-based duplicate detection (uses `difflib.SequenceMatcher`, thresholds 0.60–0.82)
- **`/manage-dependencies`** — Add/remove/query task dependencies with circular dependency prevention (DFS)
- **`/tasks`** — Opens the database in DB Browser for SQLite

### Python Scripts

- `bin/tusk-dupes.py` — Duplicate detection against open tasks (invoked via `tusk dupes`). Normalizes summaries by stripping configurable prefixes and uses `difflib.SequenceMatcher` for similarity scoring.
- `scripts/manage_dependencies.py` — Dependency graph management. Validates no self-deps and no cycles before inserting. Resolves DB path at runtime via `tusk path`.

### Database Schema

Three tables: `tasks` (13 columns — summary, status, priority, domain, assignee, task_type, priority_score, etc.), `task_dependencies` (composite PK with cascade deletes + no-self-dep CHECK), `task_sessions` (optional metrics). One view: `task_metrics` (aggregates sessions per task).

### Installation Model

`install.sh` copies `bin/tusk` → `tusk` (on PATH), skills → `.claude/skills/`, scripts → `scripts/`, and runs `tusk init`. This repo is the source; target projects get the installed copy.

## Key Conventions

- All DB access goes through `bin/tusk`, never raw `sqlite3`
- Task workflow: `To Do` → `In Progress` → `Done` (must set `closed_reason` when marking Done)
- Priority scoring: `base_priority + source_bonus + unblocks_bonus`
- Deferred tasks from PR reviews get `[Deferred]` prefix and 60-day `expires_at`
- Always run `/check-dupes` before inserting new tasks
- Dependencies use DFS cycle detection in Python; SQLite CHECK prevents self-loops
