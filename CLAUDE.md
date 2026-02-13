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

# Populate token/cost stats for a session from JSONL transcripts
bin/tusk session-stats <session_id> [transcript_path]

# Version, migration, and upgrade
bin/tusk version               # Print installed version
bin/tusk migrate               # Apply pending schema migrations
bin/tusk upgrade               # Upgrade tusk from GitHub
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
- **`/tusk-init`** — Interactive setup wizard: scans codebase, suggests domains/agents, writes config, appends CLAUDE.md snippet, seeds tasks from TODOs
- **`/tasks`** — Opens the database in DB Browser for SQLite

### Python Scripts

- `bin/tusk-dupes.py` — Duplicate detection against open tasks (invoked via `tusk dupes`). Normalizes summaries by stripping configurable prefixes and uses `difflib.SequenceMatcher` for similarity scoring.
- `bin/tusk-session-stats.py` — Token/cost tracking for task sessions (invoked via `tusk session-stats`). Parses Claude Code JSONL transcripts, deduplicates by requestId, and computes costs using per-model pricing.
- `scripts/manage_dependencies.py` — Dependency graph management. Validates no self-deps and no cycles before inserting. Resolves DB path at runtime via `tusk path`.

### Database Schema

Four tables: `tasks` (13 columns — summary, status, priority, domain, assignee, task_type, priority_score, etc.), `task_dependencies` (composite PK with cascade deletes + no-self-dep CHECK), `task_progress` (append-only checkpoint log for context recovery — stores commit hash, files changed, and next_steps after each commit so a new session can resume mid-task), `task_sessions` (optional metrics — includes `model` column for tracking which Claude model was used). One view: `task_metrics` (aggregates sessions per task).

### Installation Model

`install.sh` copies `bin/tusk` + `bin/tusk-*.py` + `VERSION` + `config.default.json` → `.claude/bin/`, skills → `.claude/skills/`, scripts → `scripts/`, and runs `tusk init` + `tusk migrate`. This repo is the source; target projects get the installed copy.

### Versioning and Upgrades

Two independent version tracks:
- **Distribution version** (`VERSION` file): a single integer incremented with each release. Copied alongside the binary on install. `tusk version` reports it; `tusk upgrade` compares local vs GitHub to decide whether to update.
- **Schema version** (`PRAGMA user_version`): tracks which migrations have been applied to the database. `tusk migrate` reads this and applies any pending migrations in order. Fresh databases from `tusk init` start at the latest schema version.

`tusk upgrade` downloads the latest tarball from GitHub, copies all files to their installed locations (never touching `tusk/config.json` or `tusk/tasks.db`), then runs `tusk migrate` to apply any schema changes.

## Key Conventions

- All DB access goes through `bin/tusk`, never raw `sqlite3`
- Task workflow: `To Do` → `In Progress` → `Done` (must set `closed_reason` when marking Done)
- Priority scoring: `base_priority + source_bonus + unblocks_bonus`
- Deferred tasks from PR reviews get `[Deferred]` prefix and 60-day `expires_at`
- Always run `/check-dupes` before inserting new tasks
- Dependencies use DFS cycle detection in Python; SQLite CHECK prevents self-loops
- Skills are discovered at Claude Code session startup — after installing or adding a new skill, you must start a new session before invoking it with `/skill-name`
