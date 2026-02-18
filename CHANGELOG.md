# Changelog

All notable changes to tusk are documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/), adapted for integer versioning.

## [Unreleased]

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
