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
bin/tusk commit <task_id> "<message>" <file1> [file2 ...] [--criteria <id>] ... [--skip-verify]
# Note: tusk commit prepends [TASK-N] to <message> automatically — do not include it yourself
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

One config key controls automatic task seeding during `/tusk-init`:

- **`project_type`** — A string key identifying the project category (e.g. `ios_app`, `python_service`). Set by `/tusk-init` Step 2e based on the user's stated project type; `null` if unset or not a fresh-project init.

```json
{
  "project_type": "ios_app"
}
```

When `/tusk-init` reaches **Step 8.5**, it looks for `.claude/bin/bootstrap/<project_type>.json`. If the file exists, it presents the listed tasks to the user for optional seeding. Bootstrap files ship with tusk under `bootstrap/` and are copied to `.claude/bin/bootstrap/` at install time. To add a new project type, add a JSON file to `bootstrap/` and bump VERSION.

`project_type` lives in `tusk/config.json` and can be updated post-install via `/tusk-update`.

#### Built-in project types and their library dependencies

Tusk ships two bootstrap files that provision tasks for adopting standalone external library repos:

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

- **Do NOT use Claude Code's built-in `TaskGet`/`TaskList` tools for tusk task lookup.** Those tools manage background agent subprocesses, not tusk tasks. Always use `bin/tusk task-get <id>` or `bin/tusk task-list` to look up tusk tasks. `tusk task-get` accepts both integer IDs (e.g. `506`) and the `TASK-NNN` display prefix form (e.g. `TASK-506`) — the DB stores integers, but both forms work.
- **Prefer `/create-task` for all task creation.** It handles decomposition, deduplication, acceptance criteria generation, and dependency proposals in one workflow. Use `bin/tusk task-insert` directly only when scripting bulk inserts or operating in an automated context where the interactive review step is not applicable.
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
- When inserting a new step into an existing numbered/lettered sequence in a skill or doc file, scan adjacent headings to confirm the result is sequential (e.g., a new "Step 3a" inserted before "Step 3b", not "Step 3d").
- **Avoid vague cross-references in skill step bodies.** Phrases like "apply the same logic as Step X.Y" or "follow the same procedure as above" get skipped by agents in practice. Always spell out the relevant instructions inline, even if it means some duplication — explicit instructions are always followed; cross-references often are not.
- **`tusk task-done` auto-marks open criteria when commits exist.** When called with `--reason completed` and open criteria remain, it scans `git log` for `[TASK-N]` commits; if any are found, all open criteria are marked done automatically. Other close reasons (`wont_do`, `duplicate`, `expired`) still require `--force` if criteria are open.
- **`tusk-session-stats.py` and `tusk-session-recalc.py` both call `update_session_stats()` from `tusk-pricing-lib.py`.** To change what session fields are written (tokens, cost, model, context tokens, `context_window`), edit `update_session_stats()` — both scripts pick up the change automatically.
- **Underscore-named bin/ files** (not matching `tusk-*.py`) must be explicitly added to three places: (1) the copy section in `install.sh`, (2) `build_manifest()` in `tusk-generate-manifest.py`, and (3) `rule18_manifest_drift()` in `tusk-lint.py`. Run `tusk generate-manifest` after adding them to regenerate MANIFEST.
- **When using `tusk_loader.load()` for companion modules**, the `.py` filename must appear as a literal string somewhere in the calling file — rule 8 scans for literal filenames to determine if a script is referenced. Add a comment on the `import tusk_loader` line, e.g.: `import tusk_loader  # loads tusk-foo.py and tusk-bar.py`
- **Source-repo-only lint rules must guard against target projects.** Any rule in `bin/tusk-lint.py` that is only meaningful inside the tusk source repo (e.g., checks on `bin/tusk`, `MANIFEST`, or other source-only files) must begin with:
  ```python
  if not os.path.isfile(os.path.join(root, "bin", "tusk")):
      return []
  ```
  This mirrors `rule8`'s pattern and prevents the rule from firing as a spurious violation in target projects (where `REPO_ROOT` is the target project root, which has no `bin/tusk` shell script).
- **Project-specific lint rules belong in `tusk-lint-extra.py`, not in `tusk-lint.py`.** `tusk upgrade` overwrites `tusk-lint.py`; any custom rules added directly to it will be silently destroyed. Instead, create `.claude/bin/tusk-lint-extra.py` (not in MANIFEST, never touched by upgrade) and define `EXTRA_RULES` as a list of `(display_name, check_function, advisory)` tuples — `tusk-lint.py` loads and appends them automatically. When `tusk upgrade` detects that the installed `tusk-lint.py` differs from the incoming version, it prints a warning pointing to this mechanism.
- **`tusk-commit.py` path resolution has three invariants that must all hold simultaneously** (Issues #363, #365, #628):
  1. `repo_root` **must** be `realpath`'d — so that a symlinked repo root (e.g. `sym_repo → real_repo`) is resolved before the prefix comparison that determines whether a file is inside the repo. Without this, the prefix check fails for any file touched in a symlinked-root repo.
  2. `abs_path` (the file being committed) **must NOT** be `realpath`'d — `git add` expects the path as the user spelled it (possibly containing symlink components). Resolving it would produce a path that `git` cannot map back to an index entry, causing `pathspec did not match any files`.
  3. `_make_relative` uses **case-insensitive prefix stripping** to handle macOS case divergence (e.g. `/Repo` vs `/repo` for the same directory). This is intentional: on macOS `os.path.realpath` does *not* canonicalize case, so an explicit `lower()`-based comparison is required. Do not replace this with a `realpath`-on-both-sides approach — that would re-introduce the Issue #365 regression.
