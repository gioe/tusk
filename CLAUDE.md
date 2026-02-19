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

# Validate config.json against the expected schema
bin/tusk validate

# Escape and quote a string for safe SQL interpolation
bin/tusk sql-quote "O'Reilly's book"   # → 'O''Reilly''s book'

# Interactive sqlite3 shell
bin/tusk shell

# Manage acceptance criteria for tasks
bin/tusk criteria add <task_id> "criterion text" [--source original|subsumption|pr_review]
bin/tusk criteria list <task_id>
bin/tusk criteria done <criterion_id>
bin/tusk criteria reset <criterion_id>

# Manage task dependencies
bin/tusk deps add <task_id> <depends_on_id> [--type blocks|contingent]
bin/tusk deps remove <task_id> <depends_on_id>
bin/tusk deps list <task_id>
bin/tusk deps dependents <task_id>
bin/tusk deps blocked
bin/tusk deps ready
bin/tusk deps all

# Run convention checks non-interactively (exits 1 on violations)
bin/tusk lint

# Populate token/cost stats for a session from JSONL transcripts
bin/tusk session-stats <session_id> [transcript_path]

# Close a session (sets duration, captures diff stats, runs session-stats)
bin/tusk session-close <session_id> [--lines-added N] [--lines-removed N] [--skip-stats]

# Bulk-close all open sessions for a task (skips git diff, defaults lines to 0)
bin/tusk session-close --task-id <task_id> [--skip-stats]

# Start working on a task (sets status, creates session, returns JSON)
bin/tusk task-start <task_id>

# Log a progress checkpoint from the latest git commit
bin/tusk progress <task_id> [--next-steps "what remains to be done"]

# Generate and open an HTML dependency DAG
bin/tusk dag [--all]

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

`config.default.json` defines domains, task_types, statuses, priorities, closed_reasons, complexity, and agents. On `tusk init`, SQLite validation triggers are **auto-generated** from the config via an embedded Python snippet in `bin/tusk`. Empty arrays (e.g., `"domains": []`) disable validation for that column. After editing config post-install, run `tusk regen-triggers` to update triggers without destroying the database (unlike `tusk init --force` which recreates the DB).

### Skills (installed to `.claude/skills/` in target projects)

- **`/next-task`** — Selects the highest-priority unblocked task and begins a full dev workflow (branching, implementation, PR)
- **`/groom-backlog`** — Auto-closes expired deferred tasks, scans for duplicates, categorizes and re-prioritizes the backlog
- **`/create-task`** — Decomposes freeform text (feature specs, meeting notes, bug reports) into structured, deduplicated tasks
- **`/check-dupes`** — Similarity-based duplicate detection (uses `difflib.SequenceMatcher`, thresholds 0.60–0.82)
- **`/manage-dependencies`** — Add/remove/query task dependencies with circular dependency prevention (DFS)
- **`/retro`** — Post-session retrospective: reviews conversation history, surfaces process improvements and tangential issues, creates follow-up tasks
- **`/reconfigure`** — Update domains, agents, task types, and other config settings post-install without losing data
- **`/tusk-init`** — Interactive setup wizard: scans codebase, suggests domains/agents, writes config, appends CLAUDE.md snippet, seeds tasks from TODOs or project description
- **`/lint-conventions`** — Checks codebase against Key Conventions using grep-based rules
- **`/tasks`** — Opens the database in DB Browser for SQLite
- **`/dashboard`** — Generates and opens an HTML task dashboard with per-task metrics
- **`/dag`** — Generates and opens an interactive Mermaid.js dependency DAG visualization
- **`/criteria`** — Manages per-task acceptance criteria (add, list, done, reset)
- **`/progress`** — Logs a progress checkpoint from the latest git commit for context recovery
- **`/tusk-insights`** — Read-only DB health audit across 6 categories with interactive Q&A recommendations
- **`/resume-task`** — Automates session recovery: detects task from branch name, gathers progress/criteria/commits, and resumes the implementation workflow

### Python Scripts

- `bin/tusk-dupes.py` — Duplicate detection against open tasks (invoked via `tusk dupes`). Normalizes summaries by stripping configurable prefixes and uses `difflib.SequenceMatcher` for similarity scoring.
- `bin/tusk-session-stats.py` — Token/cost tracking for task sessions (invoked via `tusk session-stats`). Parses Claude Code JSONL transcripts, deduplicates by requestId, and computes costs using per-model pricing.
- `bin/tusk-dag.py` — Interactive DAG visualization (invoked via `tusk dag`). Renders task dependencies as a Mermaid.js graph with status-colored nodes, complexity-based shapes, and a click-to-inspect sidebar showing per-task metrics.
- `bin/tusk-dashboard.py` — Static HTML dashboard generator (invoked via `tusk dashboard`). Queries the `task_metrics` view for per-task token counts and cost, writes a self-contained HTML file, and opens it in the browser.
- `bin/tusk-criteria.py` — Acceptance criteria management (invoked via `tusk criteria`). Supports add, list, done, and reset subcommands for per-task acceptance criteria tracking.
- `bin/tusk-deps.py` — Dependency graph management (invoked via `tusk deps`). Validates no self-deps and no cycles before inserting.
- `bin/tusk-task-start.py` — Task start consolidation (invoked via `tusk task-start`). Fetches task, checks prior progress, reuses or creates a session, sets status to In Progress, and returns a JSON blob with all details.
- `bin/tusk-progress.py` — Progress checkpoint logging (invoked via `tusk progress`). Gathers commit hash, message, and changed files from HEAD via git, then inserts a `task_progress` row. Replaces the 4-command manual checkpoint sequence.

### Database Schema

Five tables: `tasks` (15 columns — id, summary, description, status, priority, domain, assignee, task_type, priority_score, github_pr, expires_at, closed_reason, created_at, updated_at, complexity), `task_dependencies` (composite PK with cascade deletes + no-self-dep CHECK), `task_progress` (append-only checkpoint log for context recovery — stores commit hash, files changed, and next_steps after each commit so a new session can resume mid-task), `task_sessions` (optional metrics — includes `model` column for tracking which Claude model was used), `acceptance_criteria` (id, task_id, criterion, source, is_completed, created_at, updated_at — per-task criteria with source tracking and completion status). One view: `task_metrics` (aggregates sessions per task).

### Installation Model

`install.sh` copies `bin/tusk` + `bin/tusk-*.py` + `VERSION` + `config.default.json` → `.claude/bin/`, skills → `.claude/skills/`, and runs `tusk init` + `tusk migrate`. This repo is the source; target projects get the installed copy.

### Versioning and Upgrades

Two independent version tracks:
- **Distribution version** (`VERSION` file): a single integer incremented with each release. Copied alongside the binary on install. `tusk version` reports it; `tusk upgrade` compares local vs GitHub to decide whether to update.
- **Schema version** (`PRAGMA user_version`): tracks which migrations have been applied to the database. `tusk migrate` reads this and applies any pending migrations in order. Fresh databases from `tusk init` start at the latest schema version.

`tusk upgrade` downloads the latest tarball from GitHub, copies all files to their installed locations (never touching `tusk/config.json` or `tusk/tasks.db`), then runs `tusk migrate` to apply any schema changes.

### SQLite Table-Recreation Migration Template

SQLite does not support `ALTER COLUMN` or `DROP COLUMN` (on older versions). Any migration that changes column constraints, renames a column, or removes a column requires recreating the table. Use this template inside `cmd_migrate()` in `bin/tusk`:

```bash
# Migration N→N+1: <describe what changed and why>
if [[ "$current" -lt <N+1> ]]; then
  sqlite3 "$DB_PATH" "
    -- 1. Drop validation triggers (they reference the table)
    $(sqlite3 "$DB_PATH" "SELECT 'DROP TRIGGER IF EXISTS ' || name || ';' FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'validate_%';")

    -- 2. Drop dependent views
    DROP VIEW IF EXISTS task_metrics;

    -- 3. Create the new table with the updated schema
    CREATE TABLE tasks_new (
        -- ... full column definitions with updated constraints ...
    );

    -- 4. Copy data from the old table
    INSERT INTO tasks_new SELECT * FROM tasks;
    --   If columns were added/removed/reordered, list them explicitly:
    --   INSERT INTO tasks_new (col1, col2, ...) SELECT col1, col2, ... FROM tasks;

    -- 5. Drop the old table
    DROP TABLE tasks;

    -- 6. Rename the new table
    ALTER TABLE tasks_new RENAME TO tasks;

    -- 7. Recreate any indexes that were on the original table
    --   (indexes are dropped automatically when the old table is dropped)

    -- 8. Recreate dependent views
    CREATE VIEW task_metrics AS
    SELECT t.*,
        COUNT(s.id) as session_count,
        SUM(s.duration_seconds) as total_duration_seconds,
        SUM(s.cost_dollars) as total_cost,
        SUM(s.tokens_in) as total_tokens_in,
        SUM(s.tokens_out) as total_tokens_out,
        SUM(s.lines_added) as total_lines_added,
        SUM(s.lines_removed) as total_lines_removed
    FROM tasks t
    LEFT JOIN task_sessions s ON t.id = s.task_id
    GROUP BY t.id;

    -- 9. Bump schema version
    PRAGMA user_version = <N+1>;
  "

  -- 10. Regenerate validation triggers from config
  local triggers
  triggers="\$(generate_triggers)"
  if [[ -n "\$triggers" ]]; then
    sqlite3 "\$DB_PATH" "\$triggers"
  fi

  echo "  Migration <N+1>: <describe change>"
fi
```

**Key points:**

- Run all DDL inside a single `sqlite3` call so it executes within an implicit transaction — if any step fails, nothing is committed.
- Steps 1 (drop triggers) and 10 (regenerate triggers) are separated: triggers are dropped inside the SQL transaction, but regenerated afterward via the `generate_triggers` bash function.
- Always update `PRAGMA user_version` inside the SQL block, and update the `tusk init` fresh-DB version to match.
- If the table has foreign keys pointing to it (e.g., `task_dependencies.task_id → tasks.id`), SQLite will remap them automatically on `RENAME` as long as `PRAGMA foreign_keys` is OFF (the default for raw `sqlite3` calls).
- Test the migration on a copy of the database before merging: `cp tusk/tasks.db /tmp/test.db && TUSK_DB=/tmp/test.db tusk migrate`.

## Creating a New Skill

### Directory Structure

Each skill lives in its own directory under `skills/` (source) and gets installed to `.claude/skills/` in target projects:

```
skills/
  my-skill/
    SKILL.md          # Required — main entry point
    REFERENCE.md      # Optional — companion file loaded on demand
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

### Companion Files

Skills can include additional files beyond `SKILL.md` for reference content that doesn't need to be in the hot path. `install.sh` copies all files in the skill directory, so companion files are automatically available in target projects.

**When to use companion files:**
- The skill has subcommands or detailed reference that would bloat `SKILL.md`
- Content is only needed conditionally (e.g., a specific subcommand is invoked)

**How to reference them:** Use `Read file:` with the `<base_directory>` variable shown at the top of every loaded skill:

```
Read file: <base_directory>/SUBCOMMANDS.md
```

**Example:** The `/next-task` skill uses `SKILL.md` for the default workflow and `SUBCOMMANDS.md` for auxiliary subcommands (`done`, `view`, `list`, etc.), loaded only when needed.

### Source Repo Symlink

In the tusk source repo, `.claude/skills/` is a symlink to `skills/`. This means:

- **Edit only under `skills/`** — changes appear automatically in `.claude/skills/` via the symlink. Editing `.claude/skills/` directly can cause "file modified since read" errors.
- **Stage only `skills/` paths** — `git add .claude/skills/...` won't work because Git follows the symlink back to `skills/`. Always use `git add skills/<name>/SKILL.md`.

This symlink exists only in the source repo. Target projects that install tusk get real copies (not symlinks) via `install.sh`.

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

### Changelog

When bumping `VERSION`, also update `CHANGELOG.md` in the same commit. Add an entry under a new `## [<version>] - <YYYY-MM-DD>` heading describing what changed. Use the [Keep a Changelog](https://keepachangelog.com/) categories (`Added`, `Changed`, `Fixed`, `Removed`) and keep descriptions to one line each.

## Key Conventions

- All DB access goes through `bin/tusk`, never raw `sqlite3`
- Use `$(tusk sql-quote "...")` to safely escape user-provided text in SQL statements — never manually escape single quotes
- Task workflow: `To Do` → `In Progress` → `Done` (must set `closed_reason` when marking Done)
- Valid `closed_reason` values: `completed`, `expired`, `wont_do`, `duplicate`
- Priority scoring (WSJF): `ROUND((base_priority + source_bonus + unblocks_bonus) / complexity_weight)` where complexity_weight is XS=1, S=2, M=3, L=5, XL=8
- Complexity uses t-shirt sizes: XS (~1 quick session), S (~1 full session), M (~1–2 sessions), L (~3–5 sessions), XL (~5+ sessions). L and XL tasks trigger a warning in `/next-task` before work begins
- Deferred tasks from PR reviews get `[Deferred]` prefix and 60-day `expires_at`
- Duplicate detection uses two layers: (1) **LLM semantic review** — skills that insert tasks (`/create-task`, `/retro`) fetch the existing backlog and compare proposed tasks for conceptual overlap during analysis; (2) **heuristic pre-filter** (`tusk dupes check`) — fast, deterministic safety net that catches textual near-matches before INSERT. Both layers are needed: the LLM catches semantic duplicates the heuristic misses, while the heuristic provides a reliable fallback that doesn't depend on LLM judgment
- When a duplicate is discovered (during LLM review, `/check-dupes`, `/groom-backlog`, `/retro`, or incidentally), close the lower-priority or newer task immediately with `closed_reason = 'duplicate'` — never defer duplicate closure to a follow-up task
- Dependencies use DFS cycle detection in Python; SQLite CHECK prevents self-loops
- In SQL passed through bash, use `<>` instead of `!=` for not-equal comparisons — `!=` can cause parse errors due to shell history expansion (`!` is special in bash)
- In embedded Python (`python3 -c "..."`), avoid `', '.join(...)` or single-quoted strings directly inside f-string expressions — the quotes clash with shell delimiters and cause SyntaxError. Instead, precompute the join result into a variable and reference it in the f-string (e.g., `result = ', '.join(items)` then `f"...{result}..."`)
- Skills are discovered at Claude Code session startup — after installing or adding a new skill, you must start a new session before invoking it with `/skill-name`
