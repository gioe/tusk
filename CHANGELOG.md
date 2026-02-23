# Changelog

All notable changes to tusk are documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/), adapted for integer versioning.

## [Unreleased]

## [195] - 2026-02-23

### Removed

- `tusk branch` command and `bin/tusk-branch.py` — replaced by inline git commands in `/tusk` SKILL.md Step 2
- `tusk finalize` command and `bin/tusk-finalize.py` — replaced by explicit four-step sequence in `/tusk` FINALIZE.md Step 14

## [194] - 2026-02-22

### Added

- Added `--allow-shared-commit` flag to `tusk criteria done` to suppress the spurious shared-commit warning when multiple criteria are intentionally marked on the same commit (e.g., via `tusk commit --criteria`)
- Added `--skip-verify` passthrough in `tusk commit`: when passed, forwarded to each `tusk criteria done` call
- `tusk commit` now returns exit code 3 (instead of 0) if any criteria fail to be marked done after a successful commit

### Fixed

- `tusk commit` now requires at least one file argument (restores pre-`--criteria` behavior; empty file list produced a confusing git error rather than a usage message)

## [193] - 2026-02-22

### Added

- Added `--criteria <id>` flag (repeatable) to `tusk commit`; after a successful git commit, calls `tusk criteria done` for each specified criterion, binding it to the new commit hash atomically

## [192] - 2026-02-22

### Added

- Added `tokens_in` column to `tool_call_stats` (migration 22→23); `tusk-call-breakdown.py` now populates it from transcript data alongside `tokens_out`
- Updated `tusk-dashboard-data.py` `fetch_tool_call_stats_*` functions to include `tokens_in` in SELECTs

## [191] - 2026-02-22

### Changed

- Extracted HTML generator functions (~1,000 lines) from `tusk-dashboard.py` into new `tusk-dashboard-html.py` library module; `tusk-dashboard.py` is now 397 lines (orchestration + main only)

## [190] - 2026-02-22

### Added

- Added `PILLARS.md` defining the seven product pillars (Transparent, Accessible, Extensible, Opinionated, Autonomous, Observable, Self-Improving) as a categorization framework for backlog items

### Changed

- Renamed "Skill" tab to "Skill Runs" in the dashboard for clarity

## [189] - 2026-02-22

### Fixed

- Fixed DAG node clicks not firing: call `result.bindFunctions(container)` after Mermaid renders SVG (required by Mermaid v10+)
- Fixed dashboard footer showing wrong totals for Duration/Lines/Tokens/Cost after the Model column was added at index 7 — updated hardcoded `children[]` indices in `updateFooter`
- Fixed `allRows` selector (`tr:not(.criteria-row)` → `tr[data-task-id]`) to exclude nested `<tr>` elements from tool-call sub-tables; the stray rows crashed `updateFooter`, which prevented chart initialization and tab-switching from ever running

## [188] - 2026-02-22

### Fixed

- Fixed JS syntax errors in `tusk-dashboard-js.py` that broke all click handlers: unescaped `\n` inside a single-quoted string literal (`</tr>\n` → `</tr>\\n`) and HTML-encoded the apostrophe in a tooltip title to avoid breaking the JS string

## [187] - 2026-02-22

### Changed

- Removed boilerplate `## Arguments`, `## Integration Points`, `## Adding New Rules`, and `## What It Shows` sections from six skill files (`progress`, `criteria`, `blockers`, `lint-conventions`, `check-dupes`, `dashboard`)
- Removed WSJF scoring explanation note from `skills/tusk/SKILL.md`
- Replaced `SELECT *` with explicit column list in `skills/tusk/SUBCOMMANDS.md` view command
- Eliminated duplicate `tusk setup` call on the full retro path: moved the call to `skills/retro/SKILL.md` Step 0 (shared for both lightweight and full retro) and removed it from `skills/retro/FULL-RETRO.md` Step 2

## [186] - 2026-02-22

### Changed

- `skills/review-pr/SKILL.md`: added conditional guard to REVIEWER-PROMPT.md load — file is now only read when a non-empty diff exists and reviews have been started, saving ~1530 tokens on early-exit invocations

## [185] - 2026-02-22

### Changed

- Renamed `/next-task` skill to `/tusk` — skill directory, frontmatter `name`, and all cross-references updated throughout skills, `bin/tusk-loop.py`, `CLAUDE.md`, `DOMAIN.md`, `README.md`, and `uninstall.sh`

## [184] - 2026-02-22

### Added

- `bin/tusk-dashboard-data.py`: new data-access library module containing all 17 `fetch_*` functions and `get_connection`, extracted from `tusk-dashboard.py`

### Changed

- `tusk-dashboard.py` imports data functions from `tusk-dashboard-data` via `_load_dashboard_data_module()`, removing ~350 lines of SQL/data code from the main file

## [183] - 2026-02-22

### Fixed

- `tusk lint` Rule 13 violation count now reports the number of affected scripts (not list entries); 1 modified script now correctly prints "1 violation" instead of "2 violations"

## [182] - 2026-02-22

### Added

- `tusk lint` Rule 13 (advisory): warns when any `bin/tusk-*.py` file is modified in the working tree or committed since the last VERSION bump but VERSION has not been incremented; prints current VERSION and lists the affected scripts

## [181] - 2026-02-22

### Changed

- Extracted `generate_js()` (~1,035 lines) from `tusk-dashboard.py` into a companion module `tusk-dashboard-js.py`; main file now under 2,000 lines (1,975)

## [180] - 2026-02-22

### Changed

- Extracted `generate_css()` (~1,510 lines) from `tusk-dashboard.py` into a companion module `tusk-dashboard-css.py`; main file reduced from ~4,480 to ~2,980 lines

## [179] - 2026-02-22

### Changed

- Extracted shared `upsert_criterion_tool_stats()` helper into `tusk-pricing-lib.py`; both `tusk-call-breakdown.py` and `tusk-criteria.py` now delegate to it, eliminating duplicate INSERT...ON CONFLICT logic

## [178] - 2026-02-22

### Added

- Dashboard Skills tab now shows a project-wide tool cost aggregate panel (tools used, total calls, total cost, share-of-total bar) above the Skill Run Costs section

## [177] - 2026-02-22

### Added

- Schema migration 21→22: nullable `criterion_id` FK added to `tool_call_stats`; new `UNIQUE(criterion_id, tool_name)` constraint, index, and updated CHECK to allow criterion-only rows
- `tusk criteria done` now upserts per-tool cost rows into `tool_call_stats` with `criterion_id` set using the same transcript time window used for aggregate cost attribution
- Dashboard criterion entries show a collapsible tool-cost breakdown panel when `tool_call_stats` rows exist for that criterion; degrades gracefully on pre-migration DBs

## [176] - 2026-02-22

### Added

- Schema migration 19→20: `skill_run_id` nullable FK added to `tool_call_stats`; `session_id` made nullable; new `UNIQUE(skill_run_id, tool_name)` constraint and index
- Schema migration 20→21: `CHECK (session_id IS NOT NULL OR skill_run_id IS NOT NULL)` added to `tool_call_stats` to prevent orphaned rows
- `tusk call-breakdown --skill-run <id>` now upserts rows into `tool_call_stats` with `skill_run_id` set (was display-only)
- `tusk skill-run finish` calls `tusk call-breakdown --skill-run` to persist per-tool breakdown after each run; connection closed before subprocess to avoid SQLite locking
- Dashboard skill-run table shows collapsible tool-cost drilldown panel per row when breakdown data exists
- DOMAIN.md Tool Call Stats section updated to reflect nullable session_id, skill_run_id FK, dual UNIQUE constraints, and CHECK constraint

## [175] - 2026-02-22

### Changed

- Refactored `stat_cards_html` in `tusk-dashboard.py` to use `kpi-card`/`kpi-grid`/`kpi-label`/`kpi-value` CSS classes; removed duplicate `dash-stat-*` CSS

## [174] - 2026-02-22

### Added

- `tusk call-breakdown` command: per-tool-call cost attribution with `--task`, `--session`, and `--skill-run` scopes
- `iter_tool_call_costs()` in `tusk-pricing-lib.py` for iterating per-tool-call costs in a transcript window
- `tusk session-close` now auto-populates `tool_call_stats` rows after closing a session

## [173] - 2026-02-22

### Added

- Schema migration 19: `tool_call_stats` table for pre-computed per-tool-call cost aggregates per session

## [172] - 2026-02-22

### Changed

- `tusk dashboard` Skill Run Costs section promoted to its own top-level "Skill Runs" tab (alongside Dashboard and DAG)

## [171] - 2026-02-21

### Added

- `tusk dashboard` now renders a "Skill Run Costs" section below the KPI cards with a table of recent skill runs (ID, skill name, date, cost, tokens in/out, model, metadata) and a per-run cost bar chart for skills with 2+ runs

## [170] - 2026-02-22

### Added

- `skill_runs` table (schema migration 17→18) for tracking per-execution cost of maintenance skills
- `tusk skill-run start/finish/list` CLI command backed by `bin/tusk-skill-run.py`
- `/groom-backlog` now calls `tusk skill-run start` at Step 0 and `tusk skill-run finish` at Step 7b to record token usage and estimated cost for every run

## [169] - 2026-02-21

### Fixed

- [TASK-280] `v_ready_tasks` now excludes only `blocks`-type dependencies from readiness check; `contingent` deps no longer prevent a task from appearing ready (schema migration 15→16)
- [TASK-280] `v_chain_heads` and `v_blocked_tasks` updated to exclude contingent deps for consistency (schema migration 16→17)
- [TASK-280] `tusk chain frontier` inline query updated to match `v_ready_tasks` semantics
- [TASK-280] `/next-task` blocked/list/preview subcommand queries updated to use correct relationship_type filter and `v_ready_tasks` for consistency
- [TASK-280] CLAUDE.md and DOMAIN.md updated to document that only `blocks`-type deps affect readiness

## [167] - 2026-02-21

### Added

- [TASK-279] `tusk task-reopen <task_id> --force` — resets a stuck In Progress or Done task back to To Do by bypassing the status-transition trigger; closes open sessions and regenerates triggers via `tusk regen-triggers`

## [166] - 2026-02-21

### Added

- [TASK-265] `v_chain_heads` view — non-Done tasks with unfinished downstream dependents and no unmet upstream dependencies (chain entry points); schema migration 14→15
- [TASK-265] `v_blocked_tasks` view — non-Done tasks blocked by dependency or open external blocker, with `block_reason` and `blocking_summary` columns
- [TASK-265] `v_criteria_coverage` view — per-task `total_criteria`, `completed_criteria`, and `remaining_criteria` counts

## [165] - 2026-02-21

### Added

- [TASK-264] `v_ready_tasks` view — canonical ready-to-work definition (status='To Do', no blocking deps, no open external blockers); schema migration 13→14; consumers updated in `tusk-loop.py`, `tusk-deps.py` ready subcommand, and `/next-task` skill
- [TASK-264] `tusk deps ready` behavior narrowed: previously showed all non-Done tasks with satisfied deps (including In Progress); now shows only To Do tasks, consistent with the canonical ready-to-work definition

## [164] - 2026-02-21

### Added

- [TASK-267] Status transition validation trigger — DB-level guard in `generate_triggers()` that blocks invalid `tasks.status` regressions (`Done`→any, `In Progress`→`To Do`); schema migration 12→13

## [163] - 2026-02-21

### Added

- [TASK-271] `tusk task-start --force` — bypass the zero-criteria guard with a warning, enabling autonomous workflows to recover from misconfigured tasks

## [162] - 2026-02-21

### Added

- [TASK-268] `tusk task-start` now guards against tasks with open external blockers — exits non-zero listing each blocker and a resolve hint

## [161] - 2026-02-21

### Added

- [TASK-261] `tusk loop` CLI command and `/loop` skill — autonomous backlog loop that dispatches `/chain` for chain-head tasks and `/next-task` for standalone tasks, supports `--max-tasks N` and `--dry-run`

## [160] - 2026-02-21

### Changed

- [TASK-260] Renamed `/run-chain` skill to `/chain` (directory, frontmatter, all cross-references updated)

## [159] - 2026-02-21

### Added

- [TASK-255] `branch-naming.sh` PreToolUse hook blocks `git push` when the current branch does not match `feature/TASK-<id>-<slug>` (main, master, and release/* are always allowed)
- [TASK-255] `version-bump-check.sh` PreToolUse hook warns (advisory, exit 0) when distributable files (`bin/`, `skills/`, `config.default.json`, `install.sh`) changed since `origin/main` but `VERSION` was not bumped

## [158] - 2026-02-21

### Added

- [TASK-253] Rule 11 in `tusk-lint.py`: validates `skills/*/SKILL.md` frontmatter — checks for `---` delimiters, required fields (`name`, `description`, `allowed-tools`), and that `name` matches the directory name

## [157] - 2026-02-21

### Added

- [TASK-256] `commit-msg-format.sh` PreToolUse hook warns (advisory, exit 0) when raw `git commit -m` is used with a message that doesn't start with `[TASK-<id>]`

## [156] - 2026-02-21

### Added

- [TASK-254] Rule 9 in `tusk-lint.py`: flags tasks with `[Deferred]` prefix but no `expires_at` set
- [TASK-254] Rule 10 in `tusk-lint.py`: flags `acceptance_criteria` rows with `verification_spec` set but `criterion_type='manual'`

## [155] - 2026-02-21

### Fixed

- [TASK-251] `tusk branch` now exits non-zero (code 3) with actionable resolution instructions when `git stash pop` produces merge conflicts

## [154] - 2026-02-21

### Added

- [TASK-247] `tusk criteria skip <id> --reason <reason>` marks a criterion as deferred without blocking task closure
- [TASK-247] Schema migration 11→12: `is_deferred` and `deferred_reason` columns on `acceptance_criteria`
- [TASK-247] `tusk criteria list` shows deferred criteria with `[~]` marker and deferred reason
- [TASK-247] `run-chain` Step 5 marks deferred-to-chain criteria done after VERSION/CHANGELOG consolidation

## [153] - 2026-02-21

### Changed

- Dashboard model column now sorts model names by most-recently-used first via derived-table subquery (SQLite does not support ORDER BY in GROUP_CONCAT)

## [152] - 2026-02-21

### Added

- [TASK-240] Schema migration 10→11: `code_reviews` and `review_comments` tables with validation triggers for category/severity config keys
- [TASK-241] `tusk review` CLI with subcommands: start, add-comment, list, resolve, approve, request-changes, status, summary
- [TASK-242] `/review-pr` skill with parallel AI reviewer orchestration, fix loop, and deferred task creation
- [TASK-243] Mode-aware review dispatch in `/next-task` FINALIZE.md (`disabled`, `ai_only`, `ai_then_human`)
- [TASK-244] CLAUDE.md documentation for review feature (tables, CLI, skill, config keys)

## [151] - 2026-02-21

### Added

- `tusk lint` Rule 8: warns when a `tusk-*.py` file exists in `bin/` but has no reference in the `bin/tusk` dispatcher (catches orphaned scripts after dispatcher cleanup)

## [150] - 2026-02-21

### Removed

- Standalone `tusk dag` CLI command and `bin/tusk-dag.py` (DAG visualization is now part of the dashboard)
- `/dag` skill (consolidated into `/dashboard`)

## [149] - 2026-02-20

### Added

- Manual dark mode toggle button (sun/moon icon) in dashboard header with localStorage persistence
- Dashboard footer showing generation timestamp and tusk version
- Responsive breakpoints for tablet (900px) and mobile (600px) — KPI cards stack, low-priority columns hide

### Changed

- Dark mode CSS converted from `prefers-color-scheme` media queries to `[data-theme]` attribute for manual toggle support
- Chart.js charts re-render with correct theme colors on toggle
- Added hover states and transitions on KPI cards, filter chips, and table rows

## [148] - 2026-02-20

### Changed

- Dashboard criteria panel rewritten to use client-side JSON rendering instead of server-side HTML string building

## [147] - 2026-02-20

### Added

- Dashboard filter bar expanded with domain, complexity, and task type dropdowns populated from task data
- Active filter count badge with Clear all button
- Multi-dimensional AND filtering across status, domain, complexity, type, and search
- URL hash state persistence for filter, sort, page number, and page size — restored on page load

## [146] - 2026-02-20

### Changed

- Dashboard charts replaced inline SVG with Chart.js (loaded from CDN) for cost trend and completion trend visualizations
- Added cost-by-domain doughnut chart to dashboard

## [145] - 2026-02-20

### Added

- `tusk criteria done` warns on stderr when the captured commit hash matches another completed criterion on the same task, nudging agents to commit separately per criterion for accurate cost attribution

## [144] - 2026-02-20

### Added

- `commit_hash` column on `acceptance_criteria` — automatically captures `git rev-parse --short HEAD` when a criterion is marked done via `tusk criteria done`
- `tusk criteria list` displays commit hash column
- `tusk criteria reset` clears commit_hash to NULL
- Dashboard renders commit hash as a clickable GitHub link (when task has a PR URL) or plain monospace badge
- Schema migration 8→9 adds `commit_hash TEXT` column to `acceptance_criteria`

## [143] - 2026-02-20

### Fixed

- Acceptance criteria `completed_at` now uses millisecond precision (`strftime('%Y-%m-%d %H:%M:%f')`) instead of second-level `datetime('now')`, fixing zero-width cost windows and indistinguishable completion order
- `parse_sqlite_timestamp` in pricing lib handles both second-level and millisecond-level timestamps

## [142] - 2026-02-20

### Added

- Typed acceptance criteria with automated verification: `criterion_type` field (manual, code, test, file) on acceptance criteria with `verification_spec` and `verification_result` columns
- `tusk criteria add` accepts `--type` and `--spec` flags for creating typed criteria
- `tusk criteria done` runs automated verification for non-manual types (shell command for code/test, glob check for file) and blocks on failure; `--skip-verify` bypasses verification
- `tusk criteria list` shows Type column
- `tusk criteria reset` clears `verification_result`
- `tusk task-insert --typed-criteria` flag for creating typed criteria atomically via JSON objects
- `tusk task-start` includes `criterion_type` and `verification_spec` in criteria output
- Schema migration 7→8 adds three columns to `acceptance_criteria` (criterion_type, verification_spec, verification_result)
- `criterion_types` config key with config-driven trigger validation

## [141] - 2026-02-20

### Changed

- Extract shared transcript/pricing utilities into `bin/tusk-pricing-lib.py` — `load_pricing()`, `resolve_model()`, `aggregate_session()`, `compute_cost()`, and related helpers are now defined once and imported by tusk-session-stats.py, tusk-criteria.py, and tusk-session-recalc.py

## [140] - 2026-02-20

### Added

- Per-criterion cost tracking on acceptance criteria: `tusk criteria done` parses the Claude transcript for the time window since the previous criterion (or session start) and stores cost_dollars, tokens_in, tokens_out, and completed_at
- Schema migration 6→7 adds four nullable columns to `acceptance_criteria` (completed_at, cost_dollars, tokens_in, tokens_out)
- `tusk criteria list` shows Cost column and total cost across completed criteria
- `tusk criteria reset` clears cost columns along with completion status
- Dashboard renders green cost badges on completed criteria with cost data

## [139] - 2026-02-19

### Changed

- `pricing.json` now carries both `cache_write_5m` and `cache_write_1h` rates per model; `cache_write_tier` top-level field removed
- `tusk-session-stats.py` extracts per-tier cache write tokens from nested `cache_creation` object (falls back to 5m tier for older transcripts) and uses a five-term cost formula
- `tusk pricing-update` emits both cache write rates per model; `--cache-tier` flag removed

### Added

- `tusk session-recalc` command to re-run cost calculations for all existing sessions after pricing changes

## [138] - 2026-02-19

### Added

- `tusk pricing-update` command to fetch and update pricing.json from Anthropic docs with HTML table parsing, human-readable diff output, `--dry-run` mode, and `--cache-tier` option (5m default, 1h available)

## [137] - 2026-02-19

### Added

- `tusk task-done` checks for uncompleted acceptance criteria before closing; warns and exits non-zero unless `--force` is passed
- `tusk finalize` passes `--force` to `tusk task-done` to preserve existing behavior

## [136] - 2026-02-19

### Added

- Acceptance criteria IDs (e.g., `#42`) displayed in dashboard collapsible criteria rows for easy CLI reference

## [135] - 2026-02-19

### Added

- Collapsible rows in the HTML dashboard that expand to show acceptance criteria (text, completion status, source) per task

## [134] - 2026-02-19

### Added

- `tusk branch` auto-stashes dirty working tree before checkout/pull and restores changes on the new feature branch

## [133] - 2026-02-19

### Added

- Dashboard now includes "Started" (created_at) and "Last Updated" (updated_at) date columns with YYYY-MM-DD formatting and default sort by Last Updated descending

## [132] - 2026-02-19

### Fixed

- `tusk token-audit` companion file analysis: expanded CONDITIONAL_KEYWORDS regex to recognize `→` arrows, `for each` loops, `after` sequencing, and `follow` navigation; extended context window to look forward 3 lines — eliminates all false-positive UNCONDITIONAL flags

## [131] - 2026-02-19

### Added

- `tusk token-audit` command and `/token-audit` skill — analyzes skill token consumption across five categories (size census, companion file analysis, SQL anti-patterns, redundancy detection, narrative density) with `--summary` and `--json` output modes

## [130] - 2026-02-19

### Added

- `tusk sync-skills` command — regenerates `.claude/skills/` per-skill symlinks from `skills/` (public) and `skills-internal/` (private) directories
- `skills-internal/` directory for dev-only skills that are available in the source repo but excluded from `install.sh` distribution

### Changed

- `.claude/skills/` is now a real directory with per-skill symlinks instead of a single directory symlink to `skills/`

## [129] - 2026-02-19

### Removed

- Deleted orphaned `AUTO-CLOSE.md` from groom-backlog skill (replaced by `tusk autoclose` in v127)

## [128] - 2026-02-19

### Added

- `tusk finalize` command — consolidates post-merge sequence (set PR URL, close session, merge PR, mark task Done) into a single call

### Changed

- `/next-task` FINALIZE.md Steps 12-14 simplified: Step 12 uses `tusk task-update` instead of raw SQL, Step 14 uses single `tusk finalize` call instead of 3 separate commands

## [127] - 2026-02-19

### Added

- `tusk autoclose` command — runs all three groom-backlog pre-checks (expired deferred, merged PRs, moot contingent) in a single call with JSON summary output

### Changed

- `/groom-backlog` pre-check step now calls `tusk autoclose` instead of a counting query + AUTO-CLOSE.md companion file read + multi-step loops

## [126] - 2026-02-19

### Added

- `tusk task-update` command — validates enum fields against config and updates only specified columns, replacing model-composed UPDATE SQL in skills

### Changed

- `/groom-backlog` Step 5 and Step 6d now use `tusk task-update` instead of raw UPDATE SQL for priority, assignee, and complexity changes

## [125] - 2026-02-19

### Added

- `tusk task-insert` command — validates enums against config, runs dupe check, and inserts task + criteria in one transaction, replacing 4-6 tool calls per task created

### Changed

- `/create-task`, `/retro`, and `/next-task` FINALIZE.md now use `tusk task-insert` instead of manual INSERT SQL + criteria loop

## [124] - 2026-02-19

### Added

- `tusk branch <task_id> <slug>` command — detects default branch, checks out and pulls latest, creates `feature/TASK-<id>-<slug>` branch in one step

### Changed

- `/next-task` skill Step 2 now calls `tusk branch` instead of 4 sequential git commands

## [123] - 2026-02-19

### Added

- `tusk setup` command — returns config, backlog, and conventions as a single JSON object, replacing 3 separate tool calls

### Changed

- `/create-task`, `/groom-backlog`, and `/retro` now call `tusk setup` instead of separate `tusk config`, backlog query, and `tusk conventions` calls

## [122] - 2026-02-19

### Added

- `tusk commit <task_id> "<message>" [files...]` command — runs lint (advisory), stages files, and commits with `[TASK-<id>]` format and Co-Authored-By trailer in one step

### Changed

- `/next-task` skill Step 7 now references `tusk commit` instead of separate lint/add/commit steps

## [121] - 2026-02-19

### Added

- `tusk task-done <task_id> --reason <closed_reason>` command — consolidates task closure into a single call (closes open sessions, sets Done + closed_reason, reports newly unblocked tasks as JSON)

### Changed

- `/next-task` FINALIZE.md, SUBCOMMANDS.md, `/groom-backlog`, and `/run-chain` now use `tusk task-done` instead of raw SQL UPDATEs for task closure

## [120] - 2026-02-19

### Changed

- `tusk task-start` now includes a `criteria` key in its JSON output with the task's acceptance criteria
- `/next-task`, `/resume-task`, and `/run-chain` no longer call `tusk criteria list` separately — criteria come from `tusk task-start`

## [119] - 2026-02-19

### Added

- `tusk wsjf` command — encapsulates the WSJF priority scoring SQL in a single CLI call, replacing the fragile pattern of reading the formula from a companion file

### Changed

- `/groom-backlog` Step 7 now calls `tusk wsjf` instead of reading REFERENCE.md for the scoring SQL

## [118] - 2026-02-19

### Added

- Estimation accuracy insights to dashboard — complexity tier rows now show expected session ranges alongside actuals with warning flags when tiers exceed expectations, and a per-task deviation column highlights how far each completed task's sessions differ from its tier average (outliers > +100% highlighted in red)

## [117] - 2026-02-19

### Added

- Cost trend visualization to the HTML dashboard — weekly cost bar chart with cumulative cost line, dual Y-axes, hover tooltips, and empty state handling (pure inline SVG, no external dependencies)

## [116] - 2026-02-19

### Added

- Client-side sorting, filtering, and pagination to the HTML task dashboard — click-to-sort columns, status filter chips, text search, page-size selector (25/50/All), and dynamic footer totals

## [115] - 2026-02-19

### Added

- Post-chain retro aggregation step (Step 6) in `/run-chain` that reads all agent transcript output files, extracts friction points/workarounds/tangential issues/failed approaches/conventions, identifies cross-agent patterns, and presents a consolidated retro report with proposed actions (tasks and conventions)

## [114] - 2026-02-19

### Changed

- `/run-chain` agent prompt now instructs agents to skip VERSION/CHANGELOG bumps to avoid merge conflicts in parallel waves
- Added VERSION & CHANGELOG consolidation step (Step 5) to `/run-chain` that performs a single bump after the entire chain completes

## [113] - 2026-02-19

### Added

- Agent turn-limit recovery in `/run-chain` Steps 3 and 4d: detects when agents complete without finishing their task and offers Resume/Skip/Abort options

## [112] - 2026-02-19

### Added

- Category D (Conventions) in `/retro` skill for detecting and writing generalizable project heuristics to `tusk/conventions.md`
- Convention deduplication: retro reads existing conventions before appending to avoid duplicates
- Both lightweight (XS/S) and full (M/L/XL) retro paths now include convention detection and writing

## [111] - 2026-02-19

### Added

- `/create-task` reads `tusk/conventions.md` as preamble context before decomposition (Step 2c) so learned heuristics influence task splitting
- `/groom-backlog` reads `tusk/conventions.md` as preamble context before analysis so conventions inform grooming decisions

## [110] - 2026-02-19

### Added

- `tusk conventions` CLI command to print contents or path of the learned-heuristics store (`tusk/conventions.md`)
- `tusk init` and `tusk upgrade` now create `tusk/conventions.md` with a header and usage comment if it does not exist

## [109] - 2026-02-19

### Added

- External blocker nodes in DAG visualization with distinct flag shape and red/gray coloring for open/resolved status
- Blocker-to-task edges rendered as dash-dot-cross lines in DAG
- Blocker details in DAG sidebar when clicking task or blocker nodes
- Blockers column in dashboard table showing open blocker count per task

## [108] - 2026-02-19

### Added

- `/blockers` skill wrapping `tusk blockers` CLI subcommands for conversational external blocker management

## [107] - 2026-02-19

### Added

- `tusk blockers` CLI command with add, list, resolve, remove, blocked, and all subcommands for managing external blockers

## [106] - 2026-02-19

### Added

- `/run-chain` skill for parallel DAG execution — orchestrates background agents wave-by-wave through a dependency sub-DAG

## [105] - 2026-02-19

### Changed

- DAG view now hides fully-complete connected components (all-Done dependency chains) to reduce clutter; `--all` flag bypasses the filter

## [104] - 2026-02-19

### Added

- `tusk chain` CLI command with `scope`, `frontier`, and `status` subcommands for downstream sub-DAG operations scoped to a head task

## [103] - 2026-02-19

### Changed

- `/next-task` skill now skips tasks with unresolved external blockers (joins `external_blockers` table) in all ready-task queries (default, list, preview) and shows external blockers in the `blocked` subcommand

## [102] - 2026-02-18

### Added

- Added `external_blockers` table for tracking non-task blockers (data, approval, infra, external) with config-driven `blocker_type` validation triggers and schema migration 5→6

## [101] - 2026-02-18

### Added

- Added /resume-task skill for session recovery — detects task from branch name, gathers progress checkpoints/acceptance criteria/git log, and resumes the /next-task implementation workflow from step 4

## [100] - 2026-02-18

### Fixed

- `auto-lint.sh` PostToolUse hook now resolves the `tusk` binary dynamically instead of relying on PATH, fixing `command not found` errors when the SessionStart hook hasn't run yet

## [99] - 2026-02-18

### Changed

- Updated `cmd_upgrade()` in `bin/tusk` to deploy `.claude/hooks/` scripts and merge `settings.json` hook registrations, matching the logic added to `install.sh` in TASK-149

## [98] - 2026-02-18

### Changed

- Updated install.sh to deploy all `.claude/hooks/` scripts to target projects (not just PATH and task-context hooks)
- Hook registrations are now merged from source `settings.json` into target, preserving existing user hooks

## [97] - 2026-02-18

### Added

- Added SessionStart hook (`inject-task-context.sh`) that shows in-progress tasks and their latest progress checkpoint when a new Claude Code session starts

## [96] - 2026-02-18

### Changed

- Promoted acceptance criteria generation from sub-step (5b) to top-level Step 6 in /create-task skill
- Inlined CRITERIA.md logic directly into SKILL.md to reduce friction and prevent skipping
- Added zero-criteria guardrail in Results section that flags tasks with no acceptance criteria

## [95] - 2026-02-18

### Added

- Added /tusk-insights skill for read-only DB health audits across 6 categories (config fitness, task hygiene, dependency health, session gaps, acceptance criteria, priority scoring) with interactive Q&A recommendations

## [94] - 2026-02-18

### Added

- Added /dashboard skill for generating and opening the HTML task dashboard
- Added /dag skill for generating and opening the interactive dependency DAG
- Added /criteria skill for managing per-task acceptance criteria (add, list, done, reset)
- Added /progress skill for logging progress checkpoints from git commits

## [93] - 2026-02-18

### Added

- Added interactive project-description seeding to /tusk-init (Step 9) with companion SEED-DESCRIPTION.md — guides users through description, clarifying questions, and hands off to /create-task pipeline

## [92] - 2026-02-18

### Removed

- Removed redundant 'Common Reconfiguration Scenarios' section from reconfigure SKILL.md (~645 chars saved from hot path)

## [91] - 2026-02-18

### Changed

- Extracted /create-task Step 5b (acceptance criteria generation) into companion CRITERIA.md (~1,600 chars saved from hot path)
- Consolidated two nearly identical INSERT INTO examples into a single annotated example with inline NULL handling comments

## [90] - 2026-02-17

### Changed

- Extracted /groom-backlog auto-close Steps 0/0b/0c into companion AUTO-CLOSE.md loaded only when pre-check finds candidates (~3,400 chars saved from hot path)

## [89] - 2026-02-17

### Changed

- Deduplicated 'get next task' SQL in /next-task SKILL.md — replaced second copy (L/XL exclusion variant) with prose instruction referencing the first query (~345 chars saved)

## [88] - 2026-02-17

### Changed

- Extracted /next-task Steps 12-17 (push, PR, review loop, merge, retro) into companion FINALIZE.md loaded on demand — saves ~3,300 chars from the hot path; compressed ASCII art review loop diagram into concise list

## [87] - 2026-02-17

### Changed

- Trimmed /tusk-init SKILL.md: removed Important Guidelines section, compressed lookup tables and example outputs, folded reconfigure backup into Step 1, replaced verbose edge cases — saves ~480 tokens from always-loaded content

## [86] - 2026-02-17

### Changed

- Extracted /tusk-init Steps 7-8 (CLAUDE.md snippet, TODO seeding) into companion REFERENCE.md loaded on demand — defers ~875 tokens from the hot path

## [85] - 2026-02-17

### Added

- Pre-check short-circuit in /groom-backlog: single combined query counts expired, orphaned-PR, and moot-contingent tasks; skips Steps 0/0b/0c when their counts are zero

## [84] - 2026-02-17

### Changed

- Replaced /groom-backlog Step 1 backlog query with metadata-only (no description column), adding on-demand description fetch guidance for action candidates
- Removed Step 3 (individual task re-fetch) — redundant with Step 1 data already in context
- Replaced Step 6 per-change verification SELECTs with a single batch query after all changes
- Merged Step 8 (WSJF verification) and Step 9 (final report) into one combined query

## [83] - 2026-02-17

### Changed

- Extracted /groom-backlog Steps 7-8 (WSJF formula, complexity sizing table, scoring SQL) into companion REFERENCE.md loaded on demand — shrinks SKILL.md by 87 lines (~2k tokens)

## [82] - 2026-02-17

### Changed

- Trimmed /create-task example tables: proposed-tasks (Step 4) from 4→2 rows, dependency proposals (DEPENDENCIES.md) from 3→2 rows — saves ~400 tokens per invocation

## [81] - 2026-02-17

### Changed

- Added single-task fast path in /create-task: compact inline confirmation replaces the full table+details format when only 1 task is proposed
- Step 5c (dependency proposals) is now skipped entirely for single-task invocations, reducing ceremony for the most common use case

## [80] - 2026-02-17

### Changed

- Conditionalized Step 6 backlog dump in /create-task: full backlog only shown when >3 tasks created, otherwise displays a count to save tokens

## [79] - 2026-02-17

### Changed

- Split /create-task Step 5c dependency logic into DEPENDENCIES.md companion file, saving ~1,000 input tokens on single-task invocations

## [78] - 2026-02-17

### Added

- Added `tusk dag` command for interactive Mermaid.js DAG visualization of task dependencies with click-to-inspect sidebar

## [77] - 2026-02-17

### Changed

- Changed /next-task to fetch acceptance criteria upfront (step 2) and mark them done during implementation instead of bulk-marking at finalization

## [76] - 2026-02-17

### Changed

- Split /retro skill into SKILL.md + FULL-RETRO.md companion file, reducing always-loaded size from 17KB to 3KB
- Condensed full retro content by deduplicating INSERT template, trimming Step 4/5c, and removing CLAUDE.md-redundant guidelines

## [75] - 2026-02-17

### Fixed

- Fixed task-start returning stale status in JSON output

## [74] - 2026-02-17

### Added

- Added lightweight retro mode for XS/S complexity tasks

## [73] - 2026-02-17

### Changed

- Changed per-field tusk config calls to single `tusk config` in skills

## [72] - 2026-02-17

### Changed

- Changed /next-task SKILL.md to split into lean default path and subcommand reference

## [71] - 2026-02-17

### Removed

- Removed redundant step 5b heuristic dupe check in /retro skill

## [70] - 2026-02-17

### Added

- Added `tusk progress` CLI command to consolidate checkpoint logging

## [69] - 2026-02-17

### Changed

- Changed /next-task merge-to-retro flow to be uninterrupted

## [68] - 2026-02-17

### Added

- Added `tusk task-start` CLI command to consolidate task setup steps

## [67] - 2026-02-17

### Added

- Added mandatory /retro step to /next-task

### Fixed

- Fixed dashboard overflow

## [66] - 2026-02-17

### Added

- Added empty-backlog handling to /next-task skill

## [65] - 2026-02-17

### Added

- Added optional dependency proposal step to /retro skill

## [64] - 2026-02-17

### Fixed

- Fixed lint Rule 5 false positive on SELECT queries

## [63] - 2026-02-17

### Changed

- Changed /create-task Step 5c to propose deps against existing backlog

## [62] - 2026-02-17

### Added

- Added dependency summary to /create-task Step 6 report

## [61] - 2026-02-17

### Added

- Added estimate-vs-actual complexity metrics to dashboard

## [60] - 2026-02-17

### Added

- Added dependency proposal step to /create-task skill

## [59] - 2026-02-17

### Added

- Added complexity display and L/XL warning to /next-task

## [58] - 2026-02-17

### Added

- Added lint rule: config.default.json keys must match KNOWN_KEYS

## [57] - 2026-02-17

### Fixed

- Fixed wrong column name in /next-task acceptance criteria query

## [56] - 2026-02-17

### Added

- Added complexity config fetch and INSERT column to /retro skill

## [55] - 2026-02-17

### Added

- Added acceptance criteria completion to /next-task finalization

## [54] - 2026-02-17

### Added

- Added complexity config fetch to /create-task Step 2

## [53] - 2026-02-17

### Added

- Added acceptance criteria generation to /create-task skill

## [52] - 2026-02-17

### Added

- Added bulk complexity estimation and WSJF scoring to /groom-backlog

## [51] - 2026-02-17

### Added

- Added complexity estimates to /create-task skill

## [50] - 2026-02-17

### Added

- Added complexity column to tasks table with config-driven validation

### Migration

- Added complexity column to tasks table

## [49] - 2026-02-16

### Fixed

- Fixed hardcoded tusk/tasks.db path in tusk-init skill to use $(tusk path)

## [48] - 2026-02-16

### Changed

- Changed manage_dependencies.py to route through tusk CLI as `tusk deps`

## [47] - 2026-02-16

### Added

- Added --help flag handling to tusk session-close
- Added post-commit migrate reminder to /next-task skill

### Changed

- Changed groom-backlog to use tusk session-close --task-id for expired task sessions

## [46] - 2026-02-16

### Added

- Added lint rule for Done tasks with incomplete acceptance criteria

## [45] - 2026-02-16

### Added

- Added acceptance criteria completion stats to HTML dashboard

## [44] - 2026-02-16

### Added

- Added backlog size guidance to groom-backlog skill

## [43] - 2026-02-16

### Changed

- Changed /lint-conventions skill to delegate to `tusk lint`

## [42] - 2026-02-16

### Added

- Added `tusk criteria` CLI subcommand for managing acceptance criteria

## [41] - 2026-02-16

### Added

- Added --task-id flag to session-close for bulk-closing sessions

## [40] - 2026-02-16

### Added

- Added contingent dependency penalty to groom-backlog priority scoring

## [39] - 2026-02-16

### Added

- Added session-closing SQL to Step 0 of /groom-backlog

## [38] - 2026-02-16

### Added

- Added advisory lint-conventions check to /next-task pre-PR workflow

## [37] - 2026-02-16

### Added

- Added acceptance_criteria table for structured scope tracking

### Changed

- Changed groom-backlog Step 1 queries to reduce token consumption

### Migration

- Added acceptance_criteria table (task_acceptance_criteria)

## [36] - 2026-02-16

### Added

- Added subsumption guidance to /retro skill

## [35] - 2026-02-16

### Added

- Added scope guard to /next-task skill

## [34] - 2026-02-16

### Added

- Added idempotency guard to migration 2->3

## [33] - 2026-02-16

### Added

- Added TUSK_DB env var override for testing migrations

## [32] - 2026-02-16

### Added

- Added `tusk lint` CLI command for non-interactive convention checks

## [31] - 2026-02-16

### Added

- Added `tusk session-close` command

## [30] - 2026-02-15

### Added

- Added relationship_type column to task_dependencies

### Migration

- Added relationship_type column to task_dependencies (migration 2->3)

## [29] - 2026-02-15

### Changed

- Changed heuristic dupe checker to fast pre-filter role; added LLM semantic dedup layer

## [28] - 2026-02-15

### Changed

- Changed tusk-init CLAUDE.md snippet to use bare `tusk` instead of `.claude/bin/tusk`

## [27] - 2026-02-15

### Fixed

- Fixed leading slash stripping in tokenize() for dupe detection

## [26] - 2026-02-15

### Added

- Added pre-filter duplicate step to /retro skill

## [25] - 2026-02-15

### Changed

- Changed groom-backlog to close orphaned task_sessions when auto-closing tasks

## [24] - 2026-02-15

### Added

- Added /lint-conventions skill for checking codebase conventions

## [23] - 2026-02-15

### Changed

- Changed all skill SQL examples to replace != with <>

## [22] - 2026-02-15

### Changed

- Changed /next-task to capture diff stats before merge in session close

## [21] - 2026-02-15

### Added

- Added --base $DEFAULT_BRANCH to gh pr create in /next-task skill

## [20] - 2026-02-15

### Changed

- Changed /next-task to detect default branch dynamically

## [19] - 2026-02-15

### Added

- Added SessionStart hook to put tusk on PATH automatically

## [18] - 2026-02-15

### Added

- Added version mismatch warning when local version is ahead of remote

## [17] - 2026-02-14

### Added

- Added bin/sync-skills to copy skills/ to .claude/skills/

## [16] - 2026-02-14

### Added

- Added --debug flag to Python scripts for verbose troubleshooting output

## [15] - 2026-02-14

### Added

- Added groom-backlog check for In Progress tasks with merged PRs

## [14] - 2026-02-14

### Added

- Added `tusk sql-quote` command for safe SQL string interpolation

### Changed

- Changed model pricing to externalize into pricing.json

## [13] - 2026-02-14

### Added

- Added /reconfigure skill for updating config post-install
- Added `tusk regen-triggers` command

## [12] - 2026-02-14

### Added

- Added /retro skill for post-session retrospectives

## [9] - 2026-02-14

### Added

- Added migration for task_progress table

### Migration

- Added task_progress table (migration 1->2)

## [8] - 2026-02-13

### Added

- Added /create-task skill for freeform text to structured tasks

## [7] - 2026-02-13

### Added

- Added tusk dashboard command for per-task metrics HTML view

## [5] - 2026-02-13

### Fixed

- Fixed self-replacement crash during tusk upgrade

## [4] - 2026-02-13

### Fixed

- Fixed six correctness/efficiency issues in CLI and dupe detection

## [3] - 2026-02-13

### Changed

- Changed upgrade to use GitHub Releases API
- Changed README for /tusk-init

## [2] - 2026-02-13

### Fixed

- Fixed install.sh to place binary at .claude/bin/tusk

### Added

- Added .gitignore to exclude tusk/ data directory
- Added /tusk-init interactive config wizard skill

## [1] - 2026-02-13

### Added

- Added `tusk upgrade` command with schema migrations

## [Initial Development] - 2026-02-12

### Added

- Initial commit with SQLite database, bash CLI, and install script
- CLAUDE.md project guidance
- Uninstall script
- `tusk dupes` subcommand for fuzzy duplicate detection
- task_progress table and checkpoint logging to /next-task skill
- task_sessions wiring in /next-task skill
- tusk session-stats for token/cost tracking

### Changed

- Renamed project from taskdb to tusk (final name)

### Fixed

- Fixed install/uninstall idempotency
