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

# Config + backlog + conventions in one JSON call
bin/tusk setup

# Validate config.json against the expected schema
bin/tusk validate

# Escape and quote a string for safe SQL interpolation
bin/tusk sql-quote "O'Reilly's book"   # → 'O''Reilly''s book'

# Interactive sqlite3 shell
bin/tusk shell

# Manage acceptance criteria for tasks
bin/tusk criteria add <task_id> "criterion text" [--source original|subsumption|pr_review] [--type manual|code|test|file] [--spec "verification spec"]
bin/tusk criteria list <task_id>
bin/tusk criteria done <criterion_id> [--skip-verify]
bin/tusk criteria skip <criterion_id> --reason <reason>
bin/tusk criteria reset <criterion_id>

# Downstream sub-DAG operations
bin/tusk chain scope <head_task_id>     # JSON: all downstream tasks with depths
bin/tusk chain frontier <head_task_id>  # JSON: ready tasks within scope
bin/tusk chain status <head_task_id>    # Human-readable progress summary

# Manage external blockers
bin/tusk blockers add <task_id> "<description>" [--type data|approval|infra|external]
bin/tusk blockers list <task_id>
bin/tusk blockers resolve <blocker_id>
bin/tusk blockers remove <blocker_id>
bin/tusk blockers blocked
bin/tusk blockers all

# Manage code reviews
bin/tusk review start <task_id> [--reviewer <name>] [--pass-num N] [--diff-summary text]
bin/tusk review add-comment <review_id> <text> [--file path] [--line-start N] [--line-end N] [--category cat] [--severity sev]
bin/tusk review list <task_id>
bin/tusk review resolve <comment_id> fixed|deferred|dismissed
bin/tusk review approve <review_id>
bin/tusk review request-changes <review_id>
bin/tusk review status <task_id>
bin/tusk review summary <review_id>

# Manage task dependencies
bin/tusk deps add <task_id> <depends_on_id> [--type blocks|contingent]
bin/tusk deps remove <task_id> <depends_on_id>
bin/tusk deps list <task_id>
bin/tusk deps dependents <task_id>
bin/tusk deps blocked
bin/tusk deps ready
bin/tusk deps all

# Recompute WSJF priority scores for all open tasks
bin/tusk wsjf

# Run convention checks non-interactively (exits 1 on violations)
bin/tusk lint

# Populate token/cost stats for a session from JSONL transcripts
bin/tusk session-stats <session_id> [transcript_path]

# Close a session (sets duration, captures diff stats, runs session-stats)
bin/tusk session-close <session_id> [--lines-added N] [--lines-removed N] [--skip-stats]

# Bulk-close all open sessions for a task (skips git diff, defaults lines to 0)
bin/tusk session-close --task-id <task_id> [--skip-stats]

# Start working on a task (sets status, creates session, returns JSON)
# Exits non-zero if task has zero acceptance criteria; use --force to warn and proceed anyway
bin/tusk task-start <task_id> [--force]

# Close a task (closes sessions, sets Done + closed_reason, reports unblocked tasks)
# Warns and exits non-zero if uncompleted acceptance criteria exist; use --force to override
bin/tusk task-done <task_id> --reason completed|expired|wont_do|duplicate [--force]

# Insert a task with validation, dupe check, and optional criteria in one call
bin/tusk task-insert "<summary>" "<description>" [--priority P] [--domain D] [--task-type T] [--assignee A] [--complexity C] [--criteria "..." ...] [--typed-criteria '{"text":"...","type":"...","spec":"..."}' ...] [--deferred] [--expires-in DAYS]

# Update task fields with config validation
bin/tusk task-update <task_id> [--priority P] [--domain D] [--task-type T] [--assignee A] [--complexity C] [--summary S] [--description D] [--github-pr URL]

# Auto-close expired deferred, merged-PR, and moot contingent tasks
bin/tusk autoclose

# Autonomous backlog loop: dispatch /chain or /next-task for each ready task
bin/tusk loop                    # Run until backlog is empty
bin/tusk loop --max-tasks N      # Stop after N tasks
bin/tusk loop --dry-run          # Preview without executing

# Finalize a task: set PR URL, close session, merge PR, mark Done
bin/tusk finalize <task_id> --session <session_id> --pr-url <url> --pr-number <number>

# Create a feature branch for a task
bin/tusk branch <task_id> <slug>

# Lint, stage, and commit in one step
bin/tusk commit <task_id> "<message>" <file1> [file2 ...]

# Log a progress checkpoint from the latest git commit
bin/tusk progress <task_id> [--next-steps "what remains to be done"]

# Generate and open an HTML task dashboard
bin/tusk dashboard

# Print learned-heuristics conventions file
bin/tusk conventions              # Print file contents
bin/tusk conventions --path       # Print file path

# Analyze skill token consumption
bin/tusk token-audit           # Full human-readable report
bin/tusk token-audit --summary # Top-level stats + top 5 offenders
bin/tusk token-audit --json    # Machine-readable JSON

# Skill symlink management (source repo only)
bin/tusk sync-skills           # Regenerate .claude/skills/ symlinks from skills/ + skills-internal/

# Fetch and update pricing.json from Anthropic docs
bin/tusk pricing-update            # Fetch latest prices and update pricing.json (both cache tiers)
bin/tusk pricing-update --dry-run  # Show diff without writing

# Re-run cost calculations for all existing sessions (after pricing changes)
bin/tusk session-recalc

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

`config.default.json` defines domains, task_types, statuses, priorities, closed_reasons, complexity, criterion_types, and agents. On `tusk init`, SQLite validation triggers are **auto-generated** from the config via an embedded Python snippet in `bin/tusk`. Empty arrays (e.g., `"domains": []`) disable validation for that column. After editing config post-install, run `tusk regen-triggers` to update triggers without destroying the database (unlike `tusk init --force` which recreates the DB).

The config also includes a `review` block with three keys: `mode` (`"disabled"` suppresses the `/review-pr` skill entirely), `max_passes` (maximum fix-and-re-review cycles), and `reviewers` (list of reviewer names that each get their own parallel review agent). Two additional top-level keys, `review_categories` and `review_severities`, define the valid values for comment categorization (default: `must_fix`, `suggest`, `defer`) and severity (default: `critical`, `major`, `minor`) — empty arrays disable validation for those fields.

### Skills (installed to `.claude/skills/` in target projects)

- **`/next-task`** — Selects the highest-priority unblocked task and begins a full dev workflow (branching, implementation, PR)
- **`/groom-backlog`** — Auto-closes expired deferred tasks, scans for duplicates, categorizes and re-prioritizes the backlog
- **`/create-task`** — Decomposes freeform text (feature specs, meeting notes, bug reports) into structured, deduplicated tasks
- **`/check-dupes`** — Similarity-based duplicate detection (uses `difflib.SequenceMatcher`, thresholds 0.60–0.82)
- **`/manage-dependencies`** — Add/remove/query task dependencies with circular dependency prevention (DFS)
- **`/retro`** — Post-session retrospective: reviews conversation history, surfaces process improvements and tangential issues, creates follow-up tasks, and writes generalizable conventions to `tusk/conventions.md`
- **`/reconfigure`** — Update domains, agents, task types, and other config settings post-install without losing data
- **`/tusk-init`** — Interactive setup wizard: scans codebase, suggests domains/agents, writes config, appends CLAUDE.md snippet, seeds tasks from TODOs or project description
- **`/lint-conventions`** — Checks codebase against Key Conventions using grep-based rules
- **`/tasks`** — Opens the database in DB Browser for SQLite
- **`/dashboard`** — Generates and opens an HTML task dashboard with per-task metrics
- **`/criteria`** — Manages per-task acceptance criteria (add, list, done, reset)
- **`/blockers`** — Manages external blockers for tasks (add, list, resolve, remove)
- **`/progress`** — Logs a progress checkpoint from the latest git commit for context recovery
- **`/token-audit`** — Analyzes skill token consumption across five categories (size census, companion loading, SQL anti-patterns, redundancy, narrative density)
- **`/tusk-insights`** — Read-only DB health audit across 6 categories with interactive Q&A recommendations
- **`/resume-task`** — Automates session recovery: detects task from branch name, gathers progress/criteria/commits, and resumes the implementation workflow
- **`/chain`** — Orchestrates parallel execution of a dependency sub-DAG: validates head task, displays scope tree, executes head first, then spawns parallel background agents wave-by-wave for each frontier of ready tasks, and runs a post-chain retro aggregation across all agent transcripts to surface cross-agent patterns and learnings
- **`/loop`** — Autonomous backlog loop: repeatedly picks the highest-priority ready task and dispatches it to `/chain` (if it has dependents) or `/next-task` (standalone) until the backlog is empty; supports `--max-tasks N` and `--dry-run`
- **`/review-pr`** — Runs parallel AI code reviewers against a PR diff, fixes must_fix issues, handles suggest findings interactively, and creates deferred tasks for defer findings; respects `review.mode`, `review.max_passes`, and `review.reviewers` config settings

### Python Scripts

- `bin/tusk-pricing-lib.py` — Shared transcript/pricing utilities (not a CLI command). Provides `load_pricing()`, `resolve_model()`, `parse_timestamp()`, `parse_sqlite_timestamp()`, `derive_project_hash()`, `find_transcript()`, `aggregate_session()`, `compute_cost()`, and `compute_tokens_in()`. Imported by tusk-session-stats.py, tusk-criteria.py, and tusk-session-recalc.py.
- `bin/tusk-dupes.py` — Duplicate detection against open tasks (invoked via `tusk dupes`). Normalizes summaries by stripping configurable prefixes and uses `difflib.SequenceMatcher` for similarity scoring.
- `bin/tusk-session-stats.py` — Token/cost tracking for task sessions (invoked via `tusk session-stats`). Parses Claude Code JSONL transcripts and updates session rows using shared utilities from tusk-pricing-lib.py.
- `bin/tusk-dashboard.py` — Static HTML dashboard generator (invoked via `tusk dashboard`). Queries the `task_metrics` view for per-task token counts and cost, writes a self-contained HTML file, and opens it in the browser.
- `bin/tusk-blockers.py` — External blocker management (invoked via `tusk blockers`). Supports add, list, resolve, remove, blocked (tasks with open blockers), and all subcommands. Validates blocker_type against config.
- `bin/tusk-criteria.py` — Acceptance criteria management (invoked via `tusk criteria`). Supports add, list, done, and reset subcommands for per-task acceptance criteria tracking. Criteria have a `criterion_type` (manual, code, test, file) — non-manual types run automated verification on `done` (shell command for code/test, glob check for file) and block completion on failure unless `--skip-verify` is passed. On `done`, also parses the Claude transcript for the time window since the previous criterion (or session start) and stores cost_dollars, tokens_in, tokens_out, and completed_at on the criterion row. Cost tracking is best-effort — transcript unavailability doesn't block completion.
- `bin/tusk-chain.py` — Downstream sub-DAG operations (invoked via `tusk chain`). Implements `scope` (BFS JSON dump with depths and completion counts), `frontier` (ready tasks within scope), and `status` (human-readable progress summary).
- `bin/tusk-deps.py` — Dependency graph management (invoked via `tusk deps`). Validates no self-deps and no cycles before inserting.
- `bin/tusk-task-start.py` — Task start consolidation (invoked via `tusk task-start`). Fetches task, checks prior progress, reuses or creates a session, sets status to In Progress, and returns a JSON blob with all details. Exits non-zero if the task has zero active acceptance criteria unless `--force` is passed (emits a warning but proceeds).
- `bin/tusk-task-done.py` — Task closure consolidation (invoked via `tusk task-done`). Checks for uncompleted acceptance criteria (warns and exits non-zero unless `--force`), closes open sessions, sets status to Done with closed_reason, and returns JSON with newly unblocked tasks.
- `bin/tusk-commit.py` — Atomic lint-stage-commit (invoked via `tusk commit`). Runs `tusk lint` (advisory), stages listed files, and commits with `[TASK-<id>] <message>` format and Co-Authored-By trailer.
- `bin/tusk-branch.py` — Feature branch creation (invoked via `tusk branch`). Detects default branch (remote HEAD → gh fallback → "main"), checks out and pulls latest, creates `feature/TASK-<id>-<slug>`.
- `bin/tusk-progress.py` — Progress checkpoint logging (invoked via `tusk progress`). Gathers commit hash, message, and changed files from HEAD via git, then inserts a `task_progress` row. Replaces the 4-command manual checkpoint sequence.
- `bin/tusk-task-insert.py` — Atomic task creation (invoked via `tusk task-insert`). Validates all enum fields against config, runs heuristic duplicate detection, and inserts the task row + acceptance criteria in one transaction. Supports `--typed-criteria` for non-manual criterion types with verification specs. Replaces the multi-step INSERT + sql-quote + criteria-add pattern in skills.
- `bin/tusk-task-update.py` — Task field updates with validation (invoked via `tusk task-update`). Accepts a task ID and optional flags for any updatable field, validates enum values against config, and builds a dynamic UPDATE touching only specified columns. Replaces model-composed UPDATE SQL in skills.
- `bin/tusk-autoclose.py` — Consolidated auto-close pre-checks (invoked via `tusk autoclose`). Runs three checks in one call: expired deferred tasks, To Do / In Progress tasks with merged PRs (via `gh pr view`), and moot contingent tasks. Closes each with appropriate reason and description annotation. Returns JSON summary with counts and task IDs per category.
- `bin/tusk-finalize.py` — Post-merge finalization (invoked via `tusk finalize`). Accepts task ID, session ID, PR URL, and PR number. Sets `github_pr` on the task, closes the session (capturing diff stats), merges the PR via `gh pr merge --squash --delete-branch`, and marks the task Done via `tusk task-done`. Returns JSON with task details and newly unblocked tasks.
- `bin/tusk-token-audit.py` — Skill token consumption analyzer (invoked via `tusk token-audit`). Scans skill directories and reports five categories: size census (lines + estimated tokens per skill), companion file analysis (conditional vs unconditional loading), SQL anti-patterns, redundancy detection (duplicate commands, setup + re-fetch), and narrative density (prose:code ratio). Supports `--summary` and `--json` output modes.
- `bin/tusk-loop.py` — Autonomous backlog loop (invoked via `tusk loop`). Queries the highest-priority ready task, checks if it is a chain head (via `tusk chain scope`), dispatches to `claude -p /chain <id>` or `claude -p /next-task <id>`, and repeats until the backlog is empty or a stop condition is met. Supports `--max-tasks N` and `--dry-run`.
- `bin/tusk-sync-skills.py` — Skill symlink regeneration (invoked via `tusk sync-skills`). Removes all existing symlinks in `.claude/skills/`, then creates one per skill directory found in `skills/` (public) and `skills-internal/` (private). Source-repo only — not used in target projects.
- `bin/tusk-pricing-update.py` — Pricing updater (invoked via `tusk pricing-update`). Fetches the Anthropic pricing page, parses the model pricing HTML table, builds a new models dict with both `cache_write_5m` and `cache_write_1h` rates per model, prunes stale aliases, shows a human-readable diff, and writes updated `pricing.json` (or skips with `--dry-run`).
- `bin/tusk-session-recalc.py` — Bulk session recalculation (invoked via `tusk session-recalc`). Iterates all task_sessions, finds matching transcripts, and recomputes tokens/cost with the current pricing formula. Useful after pricing.json changes.
- `bin/tusk-review.py` — Code review management (invoked via `tusk review`). Supports start, add-comment, list, resolve, approve, request-changes, status, and summary subcommands. Validates comment categories and severities against `review_categories` and `review_severities` config keys. Works with the `code_reviews` and `review_comments` tables.

### Database Schema

Seven tables: `tasks` (15 columns — id, summary, description, status, priority, domain, assignee, task_type, priority_score, github_pr, expires_at, closed_reason, created_at, updated_at, complexity), `task_dependencies` (composite PK with cascade deletes + no-self-dep CHECK), `task_progress` (append-only checkpoint log for context recovery — stores commit hash, files changed, and next_steps after each commit so a new session can resume mid-task), `task_sessions` (optional metrics — includes `model` column for tracking which Claude model was used), `acceptance_criteria` (id, task_id, criterion, source, is_completed, created_at, updated_at, completed_at, cost_dollars, tokens_in, tokens_out, criterion_type, verification_spec, verification_result, commit_hash, committed_at, is_deferred, deferred_reason — per-task criteria with source tracking, completion status, per-criterion cost tracking, typed automated verification, and deferred-to-chain flag with reason), `code_reviews` (id, task_id, reviewer, status, review_pass, diff_summary, cost_dollars, tokens_in, tokens_out, created_at, updated_at — one row per reviewer per pass, status is `pending`/`approved`/`changes_requested`, review_pass tracks which iteration of the fix-and-re-review cycle this belongs to), `review_comments` (id, review_id, file_path, line_start, line_end, category, severity, comment, resolution, deferred_task_id, created_at, updated_at — individual findings within a review, resolution is `pending`/`fixed`/`deferred`/`dismissed`, deferred_task_id links to a tusk task created for out-of-scope issues). Five views: `task_metrics` (aggregates sessions per task), `v_ready_tasks` (canonical ready-to-work definition — status = 'To Do', no blocking unfinished dependencies, no open external blockers; used by `/next-task`, `tusk-loop.py`, and `tusk-deps.py` ready subcommand), `v_chain_heads` (non-Done tasks that have unfinished downstream dependents but no unmet upstream dependencies — entry points for `/chain`), `v_blocked_tasks` (non-Done tasks blocked by either an unfinished dependency or an open external blocker, with `block_reason` and `blocking_summary` columns), `v_criteria_coverage` (per-task aggregation of `total_criteria`, `completed_criteria`, and `remaining_criteria` counts).

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

### Trigger-Only Migrations

Some migrations only need to recreate validation triggers (e.g., after adding a new valid enum value to a config-driven column). These don't require table recreation, but they still need a version bump.

**Critical rule: bump `user_version` inside the same `sqlite3` call as trigger recreation — never before it.**

```bash
# Migration N→N+1: <describe what changed — e.g., add new domain value>
if [[ "$current" -lt <N+1> ]]; then
  local triggers
  triggers="$(generate_triggers)"
  sqlite3 "$DB_PATH" "
    -- 1. Drop existing validation triggers
    $(sqlite3 "$DB_PATH" "SELECT 'DROP TRIGGER IF EXISTS ' || name || ';' FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'validate_%';")

    -- 2. Recreate triggers with updated config
    $triggers

    -- 3. Bump schema version (MUST be in the same call as trigger recreation)
    PRAGMA user_version = <N+1>;
  "
  echo "  Migration <N+1>: <describe change>"
fi
```

**Why ordering matters:** If you bump `user_version` in a prior `sqlite3` call and the trigger recreation call subsequently fails, the DB is stuck at the new version with the trigger missing. Future `tusk migrate` runs will skip the migration (thinking it was already applied) while the trigger remains absent. Keep the version bump and trigger recreation atomic in the same call.

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

### Source Repo Skill Symlinks

In the tusk source repo, `.claude/skills/` is a **real directory** containing per-skill symlinks. There are two source directories:

- **`skills/`** (public) — Skills distributed to target projects via `install.sh`. Each subdirectory gets a symlink `.claude/skills/<name> → ../../skills/<name>`.
- **`skills-internal/`** (private) — Dev-only skills available in the source repo but **never installed** to target projects. Each subdirectory gets a symlink `.claude/skills/<name> → ../../skills-internal/<name>`.

Run `tusk sync-skills` to regenerate all symlinks after adding or removing a skill directory. The `.gitignore` entry `.claude/skills/` ensures the symlinks themselves are not tracked — they are regenerated from the two source directories.

**Editing and staging rules:**

- **Edit only under `skills/` or `skills-internal/`** — editing `.claude/skills/` directly can cause "file modified since read" errors since those are symlinks.
- **Stage only `skills/` or `skills-internal/` paths** — `git add .claude/skills/...` won't work. Always use `git add skills/<name>/SKILL.md` or `git add skills-internal/<name>/SKILL.md`.

Target projects that install tusk get real copies (not symlinks) of `skills/` only — `skills-internal/` is never distributed.

### Checklist for Adding a Skill

**Public skill** (distributed to target projects):
1. Create `skills/<name>/SKILL.md` with frontmatter + instructions
2. Run `tusk sync-skills` to create the `.claude/skills/<name>` symlink
3. Add a one-line entry to the **Skills** list in `CLAUDE.md`
4. Bump the `VERSION` file (see below)
5. Commit, push, and PR
6. After merge, users must start a new Claude Code session to discover the skill

**Internal skill** (source repo only, not distributed):
1. Create `skills-internal/<name>/SKILL.md` with frontmatter + instructions
2. Run `tusk sync-skills` to create the `.claude/skills/<name>` symlink
3. Commit, push, and PR
4. After merge, start a new Claude Code session to discover the skill

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
- Task workflow: `To Do` → `In Progress` → `Done` (must set `closed_reason` when marking Done). Direct close `To Do` → `Done` is also allowed. All other transitions (e.g., `Done` → anything, `In Progress` → `To Do`) are blocked by a DB trigger (`validate_status_transition`)
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
