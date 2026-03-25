# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

tusker is a portable task management system for Claude Code projects. It provides a local SQLite database, a bash CLI (`bin/tusk`), Python utility scripts, and Claude Code skills to track, prioritize, and work through tasks autonomously.

When proposing, evaluating, or reviewing features, consult `docs/PILLARS.md` for design tradeoffs. The pillars define what tusk values and provide a shared vocabulary for resolving competing approaches.

## Commands

```bash
# Init / info
bin/tusk init [--force]
bin/tusk path
bin/tusk config [key]
bin/tusk setup          # config + backlog + conventions in one JSON call
bin/tusk validate

# Task lifecycle
bin/tusk task-get <task_id>        # accepts integer ID or TASK-NNN prefix form
bin/tusk task-list [--status <s>] [--domain <d>] [--assignee <a>] [--format text|json] [--all]  # list tasks (not the built-in TaskList tool)
bin/tusk task-select [--max-complexity XS|S|M|L|XL]
bin/tusk task-insert "<summary>" "<description>" [--priority P] [--domain D] [--task-type T] [--assignee A] [--complexity C] [--criteria "..." ...] [--typed-criteria '{"text":"...","type":"...","spec":"..."}' ...] [--deferred] [--expires-in DAYS]
bin/tusk task-start <task_id> [--force]
bin/tusk task-done <task_id> --reason completed|expired|wont_do|duplicate [--force]
bin/tusk task-update <task_id> [--priority P] [--domain D] [--task-type T] [--assignee A] [--complexity C] [--summary S] [--description D]
bin/tusk task-reopen <task_id> --force

# Dev workflow
bin/tusk branch <task_id> <slug>
bin/tusk commit <task_id> "<message>" "<file1>" ["<file2>" ...] [--criteria <id>] ... [--skip-verify]
# Note: tusk commit prepends [TASK-N] to <message> automatically — do not include it yourself
# Note: always quote file paths — zsh expands unquoted [brackets] as glob patterns before tusk receives them
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
bin/tusk backlog-scan [--duplicates] [--unassigned] [--unsized] [--expired]   # → {duplicates:[...], unassigned:[...], unsized:[...], expired:[...]}
bin/tusk test-detect               # → {"command": "<cmd>", "confidence": "high|medium|low|none"}
bin/tusk scaffold-reviewer-prompts # → {created:[...], skipped:[...], skill_dir_missing:bool}
bin/tusk init-fetch-bootstrap      # → {"libs": [{name, repo, tasks, error}, ...]}
bin/tusk init-write-config [--domains <json>] [--agents <json>] [--task-types <json>] [--test-command <cmd>] [--project-type <type>] [--project-libs <json>]  # → {"success": bool, "config_path": "...", "backed_up": bool}
bin/tusk git-default-branch        # → prints default branch name (e.g. "main"); symbolic-ref → gh fallback → "main"
bin/tusk branch-parse [--branch <name>]  # → {"task_id": N}; parses task ID from current or named branch
bin/tusk sql-quote "O'Reilly's book"   # → 'O''Reilly''s book'
bin/tusk shell

# Versioning
bin/tusk version
bin/tusk version-bump                              # increment VERSION by 1, stage, echo new version
bin/tusk changelog-add <version> [<task_id>...]   # prepend dated entry to CHANGELOG.md, echo block
bin/tusk migrate
bin/tusk regen-triggers
bin/tusk upgrade [--no-commit] [--force]  # --no-commit: skip auto-commit; --force: upgrade even if version matches or exceeds remote
```

Additional subcommands (`blockers`, `review`, `chain`, `loop`, `deps blocked/all`, `session-stats`, `session-close`, `session-recalc`, `skill-run`, `call-breakdown`, `token-audit`, `pricing-update`, `sync-skills`, `dashboard`) follow the same `bin/tusk <cmd> --help` pattern — see source or run `--help` for flags.

There is no build step or external linter in this repository.

## Running the test suite

```bash
python3 -m pytest tests/ -v          # run all tests
python3 -m pytest tests/unit/ -v     # unit tests only (pure in-memory, no subprocess)
python3 -m pytest tests/integration/ -v  # integration tests only (requires a working tusk installation)
```

Integration tests initialize their own temporary database automatically via a pytest fixture — no manual `tusk init` is needed.

Dev dependencies (pytest) are listed in `requirements-dev.txt`. Install with:

```bash
pip install -r requirements-dev.txt
```

Tests live under `tests/unit/` (pure in-memory, no subprocess) and `tests/integration/` (spin up a real DB via `tusk init`). Add new tests in the appropriate subdirectory following the existing patterns.

### macOS case-insensitive filesystem: realpath does NOT canonicalize case

On macOS, `os.path.realpath` resolves symlinks but **does not** canonicalize letter case. A path like `/Repo/src` and `/repo/src` may refer to the same directory, but `realpath` will return whichever case you passed in — unchanged. Do **not** mock `os.path.realpath` to simulate case canonicalization in macOS filesystem tests (e.g., mapping a wrong-case path to its canonical form). That behavior does not exist on macOS and produces false-positive test results. To test case-insensitive FS handling, use `@pytest.mark.skipif(sys.platform != "darwin", ...)` and exercise the actual path-comparison logic (e.g., `_escapes_root()`) directly.

## Architecture

### Single Source of Truth: `bin/tusk`

The bash CLI resolves all paths dynamically. The database lives at `<repo_root>/tusk/tasks.db`. Everything references `bin/tusk` — skills call it for SQL, Python scripts call `subprocess.check_output(["tusk", "path"])` to resolve the DB path. Never hardcode the database path.

### Config-Driven Validation

`config.default.json` defines domains, task_types, statuses, priorities, closed_reasons, complexity, criterion_types, and agents. On `tusk init`, SQLite validation triggers are **auto-generated** from the config via an embedded Python snippet in `bin/tusk`. Empty arrays (e.g., `"domains": []`) disable validation for that column. After editing config post-install, run `tusk regen-triggers` to update triggers without destroying the database (unlike `tusk init --force` which recreates the DB).

The config also includes a `review` block: `mode` (`"disabled"` or `"ai_only"`), `max_passes`, and `reviewers`. Top-level `review_categories` and `review_severities` define valid comment values — empty arrays disable validation.

### Project Bootstrap

Two config keys control automatic task seeding during `/tusk-init`:

- **`project_type`** — A string key identifying the project category (e.g. `ios_app`, `python_service`). Set by `/tusk-init` Step 2e based on the user's stated project type; `null` if unset or not a fresh-project init. Stored in `tusk/config.json` and can be updated post-install via `/tusk-update`.

- **`project_libs`** — A map of lib names to `{ repo, ref }` objects. Set by `/tusk-init` during Step 6. When Step 8.5 runs, each configured lib is fetched from GitHub and its tasks are optionally seeded.

```json
{
  "project_type": "ios_app",
  "project_libs": {
    "ios_app": { "repo": "gioe/ios-libs", "ref": "main" }
  }
}
```

When `/tusk-init` reaches **Step 8.5**, it fetches `tusk-bootstrap.json` from each lib's GitHub repo using the pinned `ref`:

```bash
gh api repos/<owner>/<repo>/contents/tusk-bootstrap.json?ref=<ref> --jq '.content' | base64 -d
```

If the file exists and is valid JSON (required keys: `version`, `project_type`, `tasks`), the task list is presented to the user for optional seeding. If the file doesn't exist (404) or `gh` is unavailable, that lib is silently skipped.

#### Built-in project types and their library dependencies

Two external library repos ship their own `tusk-bootstrap.json` and are pre-configured in `project_libs` by `/tusk-init`:

- **`ios_app`** — Seeds tasks for integrating [gioe/ios-libs](https://github.com/gioe/ios-libs), a standalone Swift Package Manager library repo providing SharedKit (UI design tokens and components) and APIClient (HTTP client). Tasks cover adding the SPM dependency, configuring design tokens, and wiring up APIClient with the project's OpenAPI spec.

- **`python_service`** — Seeds tasks for integrating [gioe/python-libs](https://github.com/gioe/python-libs), a standalone Python library repo distributed as the `gioe-libs` package. It provides structured logging (`gioe_libs.aiq_logging`), optional OpenTelemetry/Sentry observability extras, and shared utilities. Tasks cover installing the package, configuring structured logging, and (optionally) enabling observability.

### Skills (installed to `.claude/skills/` in target projects)

- **`/tusk`** — Full dev workflow: pick task, implement, commit, review, done, retro
- **`/groom-backlog`** — Auto-close expired tasks, dedup, re-prioritize backlog
- **`/create-task`** — Decompose freeform text into structured tasks
- **`/retro`** — Post-session retrospective; surfaces improvements and proposes tasks or lint rules
- **`/tusk-update`** — Update config post-install without losing data
- **`/tusk-init`** — Interactive setup wizard
- **`/dashboard`** — HTML task dashboard with per-task metrics
- **`/tusk-insights`** — Read-only DB health audit
- **`/investigate`** — Scope a problem via Plan Mode and propose remediation tasks for `/create-task`
- **`/resume-task`** — Recover session from branch name + progress log
- **`/chain`** — Parallel dependency sub-DAG execution (one or more head IDs)
- **`/loop`** — Autonomous backlog loop; dispatches `/chain` or `/tusk` until empty
- **`/review-commits`** — Parallel AI code review; fixes must_fix, defers suggest/defer findings

### Database Schema

See `docs/DOMAIN.md` for the full schema, views, invariants, and status-transition rules.

Twelve tables: `tasks`, `task_dependencies`, `task_progress`, `task_sessions`, `acceptance_criteria`, `code_reviews`, `review_comments`, `skill_runs`, `tool_call_stats`, `tool_call_events`, `conventions`, `lint_rules`. Five views: `task_metrics`, `v_ready_tasks`, `v_chain_heads`, `v_blocked_tasks`, `v_criteria_coverage`.

### Installation Model

`install.sh` copies `bin/tusk` + `bin/tusk-*.py` + `VERSION` + `config.default.json` → `.claude/bin/`, skills → `.claude/skills/`, and runs `tusk init` + `tusk migrate`. This repo is the source; target projects get the installed copy.

### Versioning and Upgrades

Two independent version tracks:
- **Distribution version** (`VERSION` file): a single integer incremented with each release. `tusk version` reports it; `tusk upgrade` compares local vs GitHub to decide whether to update.
- **Schema version** (`PRAGMA user_version`): tracks which migrations have been applied. `tusk migrate` applies pending migrations in order.

`tusk upgrade` downloads the latest tarball from GitHub, copies all files to their installed locations (never touching `tusk/config.json` or `tusk/tasks.db`), then runs `tusk migrate`.

### Migrations

See `docs/MIGRATIONS.md` for table-recreation and trigger-only migration templates, including the ordering rules and gotchas.

**Checklist when adding migration N:**
- Add the migration block inside `cmd_migrate()` in `bin/tusk`
- Stamp `PRAGMA user_version = N` in `cmd_init()` (the standalone sqlite3 call near the end) so that fresh installs never need to run that migration
- Update `docs/DOMAIN.md` to reflect any schema, view, or trigger changes introduced by the migration

## Creating a New Skill

See `docs/SKILLS.md` for directory structure, frontmatter format, body guidelines, companion files, and symlink mechanics.

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

## Reference Docs

- **`docs/SCRIPTS.md`** — Reference for all `bin/tusk-*.py` helper scripts: purpose, inputs, outputs, and usage examples.
- **`docs/tusk-flows.md`** — Visual and narrative description of the main tusk workflows (task lifecycle, session flow, merge flow).
- **`docs/GLOSSARY.md`** — Canonical one-sentence definitions for key tusk terms (WSJF, deferred, contingent, compound blocking, chain head, closed_reason, criterion, v_ready_tasks, session, skill run).

## Key Conventions

Fetch conventions on demand using a topic relevant to what you're about to do:

```bash
tusk conventions search <topic>
```

**When to search:**
- Before writing a commit message → `tusk conventions search commit`
- Before choosing a file location or module structure → `tusk conventions search structure`
- Before editing or creating a skill → `tusk conventions search skill`
- Before writing or modifying tests → `tusk conventions search testing`
- Before adding a migration → `tusk conventions search migration`

Use `tusk conventions list` (no filter) sparingly — only when you want a full overview of all conventions.
