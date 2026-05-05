# AGENTS.md

This file provides guidance to Codex when working with code in this repository.

## Project Overview

tusker is a portable task management system for Codex projects. It provides a local SQLite database, a bash CLI (`bin/tusk`), and Python utility scripts to track, prioritize, and work through tasks autonomously.

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
bin/tusk task-list [--status <s>] [--domain <d>] [--assignee <a>] [--workflow <w>] [--format text|json] [--all] [--include-shadows] [--bakeoff <id>]  # list tasks (not the built-in TaskList tool); bakeoff shadows are hidden by default
bin/tusk task-select [--max-complexity XS|S|M|L|XL]
bin/tusk task-insert "<summary>" "<description>" [--priority P] [--domain D] [--task-type T] [--assignee A] [--complexity C] [--workflow W] [--criteria "..." ...] [--typed-criteria '{"text":"...","type":"...","spec":"..."}' ...] [--expires-in DAYS] [--fixes-task-id ID]
bin/tusk task-start [<task_id>] [--force] [--agent NAME] [--skill NAME]   # --skill opens a skill_runs row attributed to the task (returned under result.skill_run)
bin/tusk task-done <task_id> --reason completed|expired|wont_do|duplicate [--force]
bin/tusk task-update <task_id> [--priority P] [--domain D] [--task-type T] [--assignee A] [--complexity C] [--workflow W] [--summary S] [--description D]
bin/tusk task-reopen <task_id> --force
bin/tusk task-summary <task_id> [--format json|markdown]   # end-of-run rollup: identity, cost, duration, diff, criteria counts (JSON default; markdown for user-facing display)

# Dev workflow
bin/tusk branch <task_id> <slug>
bin/tusk commit <task_id> "<message>" "<file1>" ["<file2>" ...] [--criteria <id>] ... [--skip-verify]
bin/tusk commit <task_id> "<file1>" ["<file2>" ...] -m "<message>" [--criteria <id>] ... [--skip-verify]
# Note: tusk commit prepends [TASK-N] to <message> automatically; duplicate [TASK-N] prefixes are stripped
# Note: bare -- separators are silently ignored (AI callers sometimes insert them)
# Note: always quote file paths — zsh expands unquoted [brackets] as glob patterns before tusk receives them
bin/tusk merge <task_id> [--session <session_id>] [--pr --pr-number <N>]
bin/tusk progress <task_id> [--next-steps "..."]
bin/tusk bakeoff <task_id> --models m1,m2[,mN] [--workspace-root <path>] [--claude-bin <path>] [--dry-run]  # run the same task under N models in parallel worktrees and emit a side-by-side report
bin/tusk bakeoff pick <bakeoff_id> <shadow_id> [--rebase]   # merge the chosen shadow's branch into the source task's base branch, close the source session, mark source Done (completed), and delete sibling shadow rows + worktrees. --rebase mirrors `tusk merge --rebase`: rebase chosen shadow onto default before the ff-only merge when the default branch has advanced during the bakeoff
bin/tusk bakeoff discard <bakeoff_id>            # throw every shadow for this bakeoff away — delete shadow rows + force-remove worktrees; source task left untouched

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
bin/tusk retro-signals <task_id>   # → {task_id, complexity, reopen_count, rework_chain:{fixes,fixed_by}, review_themes, skipped_criteria, tool_call_outliers, unconsumed_next_steps}
bin/tusk test-detect               # → {"command": "<cmd>", "confidence": "high|medium|low|none"}
bin/tusk add-lib [--lib <name>] [--repo <owner/repo>] [--ref <branch|tag|sha>]  # → {"lib": "<name>", "tasks": [...], "error": null}
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

**Cross-repo CWD pinning.** `bin/tusk` resolves `REPO_ROOT` by walking up from `$PWD` to the nearest `.git`. Changing CWD to a different git repo (e.g., a consumer project during a cross-repo task) would otherwise silently reroute every tusk command to that repo's database. Two env-var overrides guard against this:

- **`TUSK_PROJECT=<path>`** — pins `REPO_ROOT` (and therefore `DB_PATH`, `config.json`, etc.) to the given path regardless of CWD. Use this when working in a consumer repo while operating on the originating project's tusk DB.
- **`TUSK_DB=<path>`** — pins only the DB path (unchanged escape hatch used by migrations and tests).

When neither override is set and an active session exists for a different project (tracked in `$TUSK_STATE_DIR/active-projects`, default `~/.tusk/active-projects`), tusk emits a stderr warning listing the pinned projects and the mismatched CWD — but only when stderr is a TTY. Agent callers (Codex), piped stderr, and CI runs are silent by default, since their captured stderr lands back in LLM context and clutters it without a human to read it. `task-start` registers the current `REPO_ROOT`; `session-close` (and the bulk `--task-id` path) deregister it when no open sessions remain. `TUSK_QUIET=1` forces silence in any context; `TUSK_FORCE_WARN=1` restores the warning when stderr isn't a TTY (used by the drift regression tests).

### Config-Driven Validation

`config.default.json` defines domains, task_types, statuses, priorities, closed_reasons, complexity, criterion_types, and agents. On `tusk init`, SQLite validation triggers are **auto-generated** from the config via an embedded Python snippet in `bin/tusk`. Empty arrays (e.g., `"domains": []`) disable validation for that column. After editing config post-install, run `tusk regen-triggers` to update triggers without destroying the database (unlike `tusk init --force` which recreates the DB).

The config also includes a `review` block: `mode` (`"disabled"` or `"ai_only"`), `max_passes`, and an optional `reviewer` object (`{name, description}`). Top-level `review_categories` and `review_severities` define valid comment values — empty arrays disable validation.

**Adding a new top-level key to `config.default.json`:** You must also add the key to `KNOWN_KEYS` in `bin/tusk-config-tools.py` (line ~34). Rule 7 of the config linter validates that every key in `config.default.json` is present in `KNOWN_KEYS` — if it's missing, `tusk init` and `tusk validate` will both fail with a Rule 7 violation.

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

### Skills (Claude Code only)

> These skills are installed to `.claude/skills/` in Claude Code target projects. Codex consumers receive `.codex/prompts/<name>.md` ports — pilot ports for `tusk-init` and `create-task` ship today via `install.sh` and `tusk upgrade`; the bulk port of the remaining skills is in progress.

- **`/tusk`** — Full dev workflow: pick task, implement, commit, review, done, retro
- **`/groom-backlog`** — Auto-close expired tasks, dedup, re-prioritize backlog
- **`/create-task`** — Decompose freeform text into structured tasks
- **`/retro`** — Post-session retrospective; surfaces improvements and proposes tasks or lint rules
- **`/tusk-update`** — Update config post-install without losing data
- **`/tusk-init`** — Interactive setup wizard
- **`/tusk-insights`** — Read-only DB health audit + on-demand HTML task dashboard
- **`/investigate`** — Scope a problem via Plan Mode and propose remediation tasks for `/create-task`
- **`/investigate-directory`** — Audit a directory's purpose and alignment with the tusk client project
- **`/resume-task`** — Recover session from branch name + progress log
- **`/chain`** — Parallel dependency sub-DAG execution (one or more head IDs)
- **`/loop`** — Autonomous backlog loop; dispatches `/chain` or `/tusk` until empty
- **`/review-commits`** — Parallel AI code review; fixes must_fix, dismisses or spins suggest findings into follow-up tasks
- **`/address-issue`** — Fetch a GitHub issue, score it, create a tusk task, and work through it
- **`/ios-libs-issue`** — File an issue against the configured iOS lib repo (`project_type=ios_app` only); auto-attaches originating tusk task

### Hooks (Git-event only in Codex mode)

`install.sh` populates `hooks/git/<name>.sh` and writes `.git/hooks/<event>` dispatchers in both Claude and Codex modes. Codex installs skip `.claude/hooks/` (no PreToolUse equivalent). See `docs/HOOKS.md` for the dispatcher contract and instructions for adding a new guard.

The dispatcher at `.git/hooks/<event>` carries the `TUSK_HOOK_DISPATCHER_V1` marker for idempotent re-runs and chains any pre-existing user hook to `.git/hooks/<event>.pre-tusk` so external hooks are preserved rather than overwritten.

Per-event guard mapping (from `install.sh`):

| Event | Guards |
|-------|--------|
| `pre-commit` | `block-raw-sqlite`, `block-sql-neq`, `dupe-gate` |
| `pre-push`   | `branch-naming`, `version-bump-check` (source-repo install only — `version-bump-check` guards paths that exist only in the tusk source repo, so it's omitted in `INSTALL_ROLE=consumer` installs) |
| `commit-msg` | `commit-msg-format` |

### Database Schema

See `docs/DOMAIN.md` for the full schema, views, invariants, and status-transition rules.

### Installation Model

`install.sh` auto-detects the host agent layout and installs into the appropriate tree. See `docs/CODEX.md` for the full Claude-vs-Codex comparison.

- **Claude Code project** (`.claude/` present): copies `bin/tusk` + `bin/tusk-*.py` + `VERSION` + `config.default.json` → `.claude/bin/`, skills → `.claude/skills/`, hooks → `.claude/hooks/`, merges `.claude/settings.json`, runs `tusk init` + `tusk migrate`.
- **Codex project** (`AGENTS.md` present, no `.claude/`): copies binaries and support files → `tusk/bin/`, skips skills/hooks/settings.json (no Codex equivalents), updates `AGENTS.md` instead of `CLAUDE.md`, runs `tusk init` + `tusk migrate`.
- Neither present → `install.sh` errors out. A marker file `<install_dir>/install-mode` (contents: `claude` or `codex`) is stamped so `tusk upgrade` and `tusk init` know which mode to apply on subsequent invocations.

This repo is the source; target projects get the installed copy.

### Versioning and Upgrades

Two independent version tracks:
- **Distribution version** (`VERSION` file): a single integer incremented with each release. `tusk version` reports it; `tusk upgrade` compares local vs GitHub to decide whether to update.
- **Schema version** (`PRAGMA user_version`): tracks which migrations have been applied. `tusk migrate` applies pending migrations in order.

`tusk upgrade` downloads the latest tarball from GitHub, copies all files to their installed locations (never touching `tusk/config.json` or `tusk/tasks.db`), then runs `tusk migrate`.

### Migrations

See `docs/MIGRATIONS.md` for table-recreation and trigger-only migration templates, including the ordering rules and gotchas.

**Checklist when adding migration N:**
- Add a `migrate_N` function in `bin/tusk-migrate.py` and register it in the `MIGRATIONS` list near the bottom of that file
- Stamp `PRAGMA user_version = N` in `cmd_init()` (the standalone sqlite3 call near the end) so that fresh installs never need to run that migration
- Update `docs/DOMAIN.md` to reflect any schema, view, or trigger changes introduced by the migration
- In the idempotent-path test (`test_idempotent_when_already_at_v<N>`), explicitly stamp `PRAGMA user_version = N` on the fresh `db_path` fixture before calling `migrate_N()` — or assert `>= N` / use a `version_before` capture. Never assert `get_version(db_path) == N` without stamping: fresh DBs initialize at whatever the latest migration is, so the test breaks the moment migration N+1 lands. See `test_migrate_48.py:113` and `test_migrate_50.py:133` for the stamping pattern.
- If the migration adds/renames/removes a column on `tasks`, `task_sessions`, or `skill_runs`, also update the schema fixtures in `tests/unit/test_workflow.py` (`_TASKS_TABLE`), `tests/unit/test_dashboard_data.py` (`_SCHEMA`, `_SKILL_RUNS_TABLE`), and `tests/unit/test_skill_run_cancel.py` (`_SKILL_RUNS_TABLE`). The `TestTasksSchemaSync`, `TestTaskSessionsSchemaSync`, and `TestSkillRunsSchemaSync` guards catch drift automatically — running the unit suite will fail loudly if any fixture falls out of sync with `bin/tusk`. Other `CREATE TABLE tasks` fixtures in `tests/unit/` (e.g. `test_criteria_done.py`, `test_deps.py`, `test_lint_rule*.py`, `test_review_*.py`, `test_check_deliverables.py`) are intentional minimal subsets that declare only the columns their test queries need — they are NOT meant to mirror `bin/tusk` and need no guard or syncing when migrations add columns.
- If the migration adds, renames, or removes a column on the `tasks` table, the migration must also `DROP VIEW IF EXISTS` + `CREATE VIEW` for every view that projects `tasks` columns — currently `task_metrics`, `v_ready_tasks`, `v_chain_heads`, and `v_criteria_coverage`. SQLite resolves `SELECT t.*` at CREATE VIEW time and freezes the column list; `ALTER TABLE tasks ADD COLUMN …` does **not** propagate into these views on already-migrated DBs. Fresh installs are fine because `cmd_init` rebuilds everything end-to-end, but migrated DBs silently lose the new column from downstream view joins until the views are recreated. Copy the view SQL verbatim from the canonical definitions in `cmd_init` (`bin/tusk`) so the migrated shape matches fresh installs bit-for-bit. Migration 56 is the retroactive fix for migration 55 (`fixes_task_id`) and is the template for future tasks-column migrations. When writing `test_migrate_N` view-shape guards, pin the comparison against a frozen v(N)-era snapshot (see `tests/integration/test_migrate_56.py::_V56_VIEW_SQL`) — **do not** re-extract canonical SQL from live `cmd_init`. Any later tasks-column migration that re-CREATEs views in `cmd_init` will otherwise silently drift every prior migration-N test that compares against it (TASK-131).

## Creating a New Skill

See `docs/SKILLS.md` for directory structure, frontmatter format, body guidelines, companion files, and symlink mechanics.

**Public skill** (distributed to Claude Code target projects):
1. Create `skills/<name>/SKILL.md` with frontmatter + instructions
2. Run `tusk sync-skills` to create the `.claude/skills/<name>` symlink
3. Add a one-line entry to the **Skills** list in both `CLAUDE.md` and `AGENTS.md`
4. Bump the `VERSION` file (see below)
5. Commit, push, and PR

**Internal skill** (source repo only, not distributed):
1. Create `skills-internal/<name>/SKILL.md` with frontmatter + instructions
2. Run `tusk sync-skills` to create the `.claude/skills/<name>` symlink
3. Commit, push, and PR

## VERSION Bumps

The `VERSION` file contains a single integer that tracks the distribution version.

Bump for **any change delivered to a target project**: new/modified skill, CLI command, Python script, schema migration, `config.default.json`, or `install.sh`. **Do NOT bump** for repo-only changes (README, AGENTS.md, task database).

```bash
echo 14 > VERSION   # increment by 1
```

Commit the bump in the same branch as the feature. Also update `CHANGELOG.md` in the same commit under a new `## [<version>] - <YYYY-MM-DD>` heading. **One VERSION bump per PR.**

## Reference Docs

- **`docs/SCRIPTS.md`** — Reference for all `bin/tusk-*.py` helper scripts: purpose, inputs, outputs, and usage examples.
- **`docs/tusk-flows.md`** — Visual and narrative description of the main tusk workflows (task lifecycle, session flow, merge flow).
- **`tusk glossary`** — Canonical one-sentence definitions for key tusk terms (WSJF, contingent, compound blocking, chain head, closed_reason, criterion, v_ready_tasks, session, skill run). Query with `tusk glossary get <term>` or `tusk glossary search <topic>`. The rendered `docs/GLOSSARY.md` is generated from the table; edit definitions via `tusk glossary set-definition`, not by hand.

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
