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

# Escape and quote a string for safe SQL interpolation
bin/tusk sql-quote "O'Reilly's book"   # → 'O''Reilly''s book'

# Interactive sqlite3 shell
bin/tusk shell

# Populate token/cost stats for a session from JSONL transcripts
bin/tusk session-stats <session_id> [transcript_path]

# Generate and open an HTML task dashboard
bin/tusk dashboard

# Version, migration, and upgrade
bin/tusk version               # Print installed version
bin/tusk migrate               # Apply pending schema migrations
bin/tusk regen-triggers        # Drop and recreate validation triggers from config
bin/tusk upgrade               # Upgrade tusk from GitHub
```

There is no build step, test suite, or linter in this repository.

## Architecture

### Single Source of Truth: `bin/tusk`

The bash CLI resolves all paths dynamically. The database lives at `<repo_root>/tusk/tasks.db`. Everything references `bin/tusk` — skills call it for SQL, Python scripts call `subprocess.check_output(["tusk", "path"])` to resolve the DB path. Never hardcode the database path.

### Config-Driven Validation

`config.default.json` defines domains, task_types, statuses, priorities, closed_reasons, and agents. On `tusk init`, SQLite validation triggers are **auto-generated** from the config via an embedded Python snippet in `bin/tusk`. Empty arrays (e.g., `"domains": []`) disable validation for that column. After editing config post-install, run `tusk regen-triggers` to update triggers without destroying the database (unlike `tusk init --force` which recreates the DB).

### Skills (installed to `.claude/skills/` in target projects)

- **`/next-task`** — Selects the highest-priority unblocked task and begins a full dev workflow (branching, implementation, PR)
- **`/groom-backlog`** — Auto-closes expired deferred tasks, scans for duplicates, categorizes and re-prioritizes the backlog
- **`/create-task`** — Decomposes freeform text (feature specs, meeting notes, bug reports) into structured, deduplicated tasks
- **`/check-dupes`** — Similarity-based duplicate detection (uses `difflib.SequenceMatcher`, thresholds 0.60–0.82)
- **`/manage-dependencies`** — Add/remove/query task dependencies with circular dependency prevention (DFS)
- **`/retro`** — Post-session retrospective: reviews conversation history, surfaces process improvements and tangential issues, creates follow-up tasks
- **`/reconfigure`** — Update domains, agents, task types, and other config settings post-install without losing data
- **`/tusk-init`** — Interactive setup wizard: scans codebase, suggests domains/agents, writes config, appends CLAUDE.md snippet, seeds tasks from TODOs
- **`/tasks`** — Opens the database in DB Browser for SQLite

### Python Scripts

- `bin/tusk-dupes.py` — Duplicate detection against open tasks (invoked via `tusk dupes`). Normalizes summaries by stripping configurable prefixes and uses `difflib.SequenceMatcher` for similarity scoring.
- `bin/tusk-session-stats.py` — Token/cost tracking for task sessions (invoked via `tusk session-stats`). Parses Claude Code JSONL transcripts, deduplicates by requestId, and computes costs using per-model pricing.
- `bin/tusk-dashboard.py` — Static HTML dashboard generator (invoked via `tusk dashboard`). Queries the `task_metrics` view for per-task token counts and cost, writes a self-contained HTML file, and opens it in the browser.
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

## Creating a New Skill

### Directory Structure

Each skill lives in its own directory under `skills/` (source) and gets installed to `.claude/skills/` in target projects:

```
skills/
  my-skill/
    SKILL.md          # Required — the only file needed
```

### SKILL.md Format

Every `SKILL.md` must start with YAML frontmatter:

```yaml
---
name: my-skill
description: One-line description shown in the skill picker
allowed-tools: Bash, Read, Edit     # Comma-separated list of tools the skill needs
---
```

- **`name`**: Must match the directory name, use lowercase kebab-case
- **`description`**: Appears in the Claude Code skill list — keep it concise and action-oriented
- **`allowed-tools`**: Only request tools the skill actually uses. Common sets:
  - Read-only skills: `Bash`
  - Skills that modify files: `Bash, Read, Write, Edit`
  - Skills that search the codebase: `Bash, Read, Glob, Grep`

### Naming Conventions

- Directory and `name` field: lowercase kebab-case (e.g., `check-dupes`, `next-task`)
- The skill is invoked as `/name` (e.g., `/check-dupes`)

### Skill Body Guidelines

- Start with a `# Title` heading after the frontmatter
- Use `## Step N:` headings for multi-step workflows
- Include `bash` code blocks showing exact `tusk` commands to run
- Always use `tusk` CLI for DB access, never raw `sqlite3`
- Use `$(tusk sql-quote "...")` in any SQL that interpolates variables
- Reference other skills by name when integration points exist (e.g., "Run `/check-dupes` before inserting")

### Checklist for Adding a Skill

1. Create `skills/<name>/SKILL.md` with frontmatter + instructions
2. Add a one-line entry to the **Skills** list in `CLAUDE.md`
3. Bump the `VERSION` file (see below)
4. Commit, push, and PR
5. After merge, users must start a new Claude Code session to discover the skill

## VERSION Bumps

The `VERSION` file contains a single integer that tracks the distribution version.

### When to Bump

Bump `VERSION` for **any change that install/upgrade would deliver** to a target project:

- New or modified skill (`skills/`)
- New or modified CLI command (`bin/tusk`)
- New or modified Python script (`bin/tusk-*.py`, `scripts/`)
- New schema migration
- Changes to `config.default.json` or `install.sh`

**Do NOT bump** for changes that stay in this repo only (e.g., README edits, CLAUDE.md updates, task database changes).

### How to Bump

Increment the integer by 1:

```bash
# Read current version
cat VERSION

# Write new version (e.g., 13 → 14)
echo 14 > VERSION
```

Commit the bump in the same branch as the feature — not as a separate PR. The commit message can be standalone (`Bump VERSION to 14`) or folded into the feature commit.

## Key Conventions

- All DB access goes through `bin/tusk`, never raw `sqlite3`
- Use `$(tusk sql-quote "...")` to safely escape user-provided text in SQL statements — never manually escape single quotes
- Task workflow: `To Do` → `In Progress` → `Done` (must set `closed_reason` when marking Done)
- Priority scoring: `base_priority + source_bonus + unblocks_bonus`
- Deferred tasks from PR reviews get `[Deferred]` prefix and 60-day `expires_at`
- Always run `/check-dupes` before inserting new tasks
- Dependencies use DFS cycle detection in Python; SQLite CHECK prevents self-loops
- Skills are discovered at Claude Code session startup — after installing or adding a new skill, you must start a new session before invoking it with `/skill-name`
