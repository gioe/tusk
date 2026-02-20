# Changelog

All notable changes to tusk are documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/), adapted for integer versioning.

## [Unreleased]

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
