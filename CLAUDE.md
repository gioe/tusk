# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

tusker is a portable task management system for Claude Code projects. It provides a local SQLite database, a bash CLI (`bin/tusk`), Python utility scripts, and Claude Code skills to track, prioritize, and work through tasks autonomously.

When proposing, evaluating, or reviewing features, consult `PILLARS.md` for design tradeoffs. The pillars define what tusk values and provide a shared vocabulary for resolving competing approaches.

## Commands

```bash
# Init / info
bin/tusk init [--force]
bin/tusk path
bin/tusk config [key]
bin/tusk setup          # config + backlog + conventions in one JSON call
bin/tusk validate

# Task lifecycle
bin/tusk task-insert "<summary>" "<description>" [--priority P] [--domain D] [--task-type T] [--assignee A] [--complexity C] [--criteria "..." ...] [--typed-criteria '{"text":"...","type":"...","spec":"..."}' ...] [--deferred] [--expires-in DAYS]
bin/tusk task-start <task_id> [--force]
bin/tusk task-done <task_id> --reason completed|expired|wont_do|duplicate [--force]
bin/tusk task-update <task_id> [--priority P] [--domain D] [--task-type T] [--assignee A] [--complexity C] [--summary S] [--description D]
bin/tusk task-reopen <task_id> --force

# Dev workflow
bin/tusk branch <task_id> <slug>
bin/tusk commit <task_id> "<message>" <file1> [file2 ...] [--criteria <id>] ... [--skip-verify]
bin/tusk merge <task_id> [--session <session_id>] [--pr --pr-number <N>]
bin/tusk progress <task_id> [--next-steps "..."]

# Criteria
bin/tusk criteria add <task_id> "criterion" [--source original|subsumption|pr_review] [--type manual|code|test|file] [--spec "..."]
bin/tusk criteria list <task_id>
bin/tusk criteria done <criterion_id> [--skip-verify]
bin/tusk criteria skip <criterion_id> --reason <reason>
bin/tusk criteria reset <criterion_id>

# Dependencies
bin/tusk deps add <task_id> <depends_on_id> [--type blocks|contingent]
bin/tusk deps remove <task_id> <depends_on_id>
bin/tusk deps list <task_id>
bin/tusk deps ready

# Utilities
bin/tusk wsjf
bin/tusk lint
bin/tusk autoclose
bin/tusk sql-quote "O'Reilly's book"   # → 'O''Reilly''s book'
bin/tusk shell

# Versioning
bin/tusk version
bin/tusk migrate
bin/tusk regen-triggers
bin/tusk upgrade
```

Additional subcommands (`blockers`, `review`, `chain`, `loop`, `deps blocked/all`, `session-stats`, `session-close`, `session-recalc`, `skill-run`, `call-breakdown`, `token-audit`, `pricing-update`, `sync-skills`, `dashboard`, `conventions`) follow the same `bin/tusk <cmd> --help` pattern — see source or run `--help` for flags.

There is no build step, test suite, or linter in this repository.

## Architecture

### Single Source of Truth: `bin/tusk`

The bash CLI resolves all paths dynamically. The database lives at `<repo_root>/tusk/tasks.db`. Everything references `bin/tusk` — skills call it for SQL, Python scripts call `subprocess.check_output(["tusk", "path"])` to resolve the DB path. Never hardcode the database path.

### Config-Driven Validation

`config.default.json` defines domains, task_types, statuses, priorities, closed_reasons, complexity, criterion_types, and agents. On `tusk init`, SQLite validation triggers are **auto-generated** from the config via an embedded Python snippet in `bin/tusk`. Empty arrays (e.g., `"domains": []`) disable validation for that column. After editing config post-install, run `tusk regen-triggers` to update triggers without destroying the database (unlike `tusk init --force` which recreates the DB).

The config also includes a `review` block: `mode` (`"disabled"` or `"ai_only"`), `max_passes`, and `reviewers`. Top-level `review_categories` and `review_severities` define valid comment values — empty arrays disable validation.

### Skills (installed to `.claude/skills/` in target projects)

- **`/tusk`** — Full dev workflow: pick task, implement, commit, review, done, retro
- **`/groom-backlog`** — Auto-close expired tasks, dedup, re-prioritize backlog
- **`/create-task`** — Decompose freeform text into structured tasks
- **`/retro`** — Post-session retrospective; surfaces improvements and writes conventions
- **`/reconfigure`** — Update config post-install without losing data
- **`/tusk-init`** — Interactive setup wizard
- **`/dashboard`** — HTML task dashboard with per-task metrics
- **`/token-audit`** — Skill token consumption analysis
- **`/tusk-insights`** — Read-only DB health audit
- **`/resume-task`** — Recover session from branch name + progress log
- **`/chain`** — Parallel dependency sub-DAG execution (one or more head IDs)
- **`/loop`** — Autonomous backlog loop; dispatches `/chain` or `/tusk` until empty
- **`/review-commits`** — Parallel AI code review; fixes must_fix, defers suggest/defer findings

### Database Schema

See `DOMAIN.md` for the full schema, views, invariants, and status-transition rules.

Eleven tables: `tasks`, `task_dependencies`, `task_progress`, `task_sessions`, `acceptance_criteria`, `code_reviews`, `review_comments`, `skill_runs`, `tool_call_stats`, `conventions`, `lint_rules`. Five views: `task_metrics`, `v_ready_tasks`, `v_chain_heads`, `v_blocked_tasks`, `v_criteria_coverage`.

### Installation Model

`install.sh` copies `bin/tusk` + `bin/tusk-*.py` + `VERSION` + `config.default.json` → `.claude/bin/`, skills → `.claude/skills/`, and runs `tusk init` + `tusk migrate`. This repo is the source; target projects get the installed copy.

### Versioning and Upgrades

Two independent version tracks:
- **Distribution version** (`VERSION` file): a single integer incremented with each release. `tusk version` reports it; `tusk upgrade` compares local vs GitHub to decide whether to update.
- **Schema version** (`PRAGMA user_version`): tracks which migrations have been applied. `tusk migrate` applies pending migrations in order.

`tusk upgrade` downloads the latest tarball from GitHub, copies all files to their installed locations (never touching `tusk/config.json` or `tusk/tasks.db`), then runs `tusk migrate`.

### Migrations

See `MIGRATIONS.md` for table-recreation and trigger-only migration templates, including the ordering rules and gotchas.

## Creating a New Skill

See `SKILLS.md` for directory structure, frontmatter format, body guidelines, companion files, and symlink mechanics.

**Public skill** (distributed to target projects):
1. Create `skills/<name>/SKILL.md` with frontmatter + instructions
2. Run `tusk sync-skills` to create the `.claude/skills/<name>` symlink
3. Add a one-line entry to the **Skills** list in `CLAUDE.md`
4. Bump the `VERSION` file (see below)
5. Commit, push, and PR

**Internal skill** (source repo only, not distributed):
1. Create `skills-internal/<name>/SKILL.md` with frontmatter + instructions
2. Run `tusk sync-skills` to create the `.claude/skills/<name>` symlink
3. Commit, push, and PR

## VERSION Bumps

The `VERSION` file contains a single integer that tracks the distribution version.

Bump for **any change delivered to a target project**: new/modified skill, CLI command, Python script, schema migration, `config.default.json`, or `install.sh`. **Do NOT bump** for repo-only changes (README, CLAUDE.md, task database).

```bash
echo 14 > VERSION   # increment by 1
```

Commit the bump in the same branch as the feature. Also update `CHANGELOG.md` in the same commit under a new `## [<version>] - <YYYY-MM-DD>` heading. **One VERSION bump per PR.**

## Key Conventions

- All DB access goes through `bin/tusk`, never raw `sqlite3`
- Use `$(tusk sql-quote "...")` to safely escape user-provided text in SQL statements — never manually escape single quotes
- Task workflow: `To Do` → `In Progress` → `Done` (must set `closed_reason` when marking Done). Direct close `To Do` → `Done` is also allowed. All other transitions are blocked by a DB trigger (`validate_status_transition`)
- Valid `closed_reason` values: `completed`, `expired`, `wont_do`, `duplicate`
- Priority scoring (WSJF): `ROUND((base_priority + source_bonus + unblocks_bonus) / complexity_weight)` where complexity_weight is XS=1, S=2, M=3, L=5, XL=8
- Complexity uses t-shirt sizes: XS (~1 quick session), S (~1 full session), M (~1–2 sessions), L (~3–5 sessions), XL (~5+ sessions). L and XL tasks trigger a warning in `/tusk` before work begins
- Deferred tasks from PR reviews get `[Deferred]` prefix and 60-day `expires_at`
- Duplicate detection uses two layers: (1) **LLM semantic review** during task insertion; (2) **heuristic pre-filter** (`tusk dupes check`) before INSERT. Both layers are needed.
- When a duplicate is discovered, close the lower-priority or newer task immediately with `closed_reason = 'duplicate'` — never defer duplicate closure to a follow-up task
- Dependencies use DFS cycle detection in Python; SQLite CHECK prevents self-loops
- In SQL passed through bash, use `<>` instead of `!=` for not-equal comparisons — `!=` can cause parse errors due to shell history expansion
- In embedded Python (`python3 -c "..."`), avoid `', '.join(...)` or single-quoted strings directly inside f-string expressions — precompute the join result into a variable first
- Skills are discovered at Claude Code session startup — after installing or adding a new skill, you must start a new session before invoking it with `/skill-name`
