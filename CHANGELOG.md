# Changelog

All notable changes to tusk are documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/), adapted for integer versioning.

## [Unreleased]

## [323] - 2026-03-01

- [TASK-465] Give retro a direct skill-patch pathway for Category A findings

## [322] - 2026-03-01

- [TASK-464] Retro should apply lint rules inline instead of via tasks

## [321] - 2026-03-01

- [TASK-463] Add dupe check to review-commits defer path

## [320] - 2026-03-01

- [TASK-462] Fix tusk commit to use git add for all files including deletions

## [319] - 2026-03-01

- Fix /review-commits Step 3 and Step 8 to fall back to HEAD~1..HEAD when on the default branch (three-dot diff is always empty in that case)

## [318] - 2026-02-28

- [TASK-461] Add --metadata to /investigate skill-run finish with tasks_proposed/tasks_created counts

## [317] - 2026-02-28

- [TASK-460] Add cost tracking to /investigate via tusk skill-run start/finish

## [316] - 2026-02-28

### Added
- Rule 20 in `tusk-lint.py`: advisory check for `skills/` file modifications without a VERSION bump (mirrors Rule 13 for `bin/tusk-*.py`)
- Refactored Rule 13 to use shared `_version_bump_check` helper, eliminating duplicate logic

## [315] - 2026-02-28

- [TASK-454] [Deferred] Add --exclude-ids support to tusk task-select for loop delegation

## [314] - 2026-02-28

- [TASK-457] Fix tusk-generate-manifest.py to write both MANIFEST and .claude/tusk-manifest.json

## [313] - 2026-02-28

- [TASK-459] tusk merge sessions_closed counter is always 0 even when session is closed

## [311] - 2026-02-28

- [TASK-450] Add tusk version-bump and tusk changelog-add commands

## [310] - 2026-02-28

- Fix `backlog-scan --unassigned` and `--unsized` to filter `status = 'To Do'` only, excluding In Progress tasks from grooming output

## [309] - 2026-02-28

### Added
- [TASK-452] `tusk backlog-scan [--duplicates] [--unassigned] [--unsized] [--expired]` — consolidated grooming pre-flight scan returning JSON with all four categories; `/groom-backlog` updated to use it instead of individual DB queries

## [308] - 2026-02-28

### Added
- [TASK-451] `tusk test-detect` — inspects repo root lockfiles and returns `{"command": "<cmd>", "confidence": "high|medium|low|none"}`; `/tusk-init` Step 5b and `/tusk-update` Step 2b updated to call it instead of duplicating prose heuristics

## [307] - 2026-02-28

### Added
- [TASK-455] Rule 19 in `tusk-lint.py`: flags divergence between `MANIFEST` and `.claude/tusk-manifest.json`; also fixed existing drift (missing `investigate/SKILL.md` entry)

## [306] - 2026-02-28

### Added
- [TASK-449] `tusk task-select [--max-complexity XS|S|M|L|XL]` — returns top WSJF-ranked ready task as JSON; updated `/tusk` and `/loop` SKILL.md to use it instead of inline SELECT queries

## [305] - 2026-02-28

### Added
- [TASK-453] `tusk chain frontier-check <ids>` — single call returning `{status, frontier}` for loop-termination detection; updated `/chain` SKILL.md Step 4b to use it

## [304] - 2026-02-28

- Fix hardcoded `tusk` binary path in `tusk-task-insert.py` and `tusk-task-update.py`; resolve via `__file__`-relative path like other scripts

## [303] - 2026-02-28

### Fixed
- `tusk-pricing-lib.py`: `find_transcript()` now falls back through git root and parent directories when `os.getcwd()` hash yields no JSONL transcripts — fixes failure when running tusk from a subdirectory of a project
- `tusk-session-recalc.py`: uses `find_all_transcripts_with_fallback()` for the same multi-candidate discovery
- `tusk-session-stats.py`: calls `find_transcript()` without explicit hash so the new fallback logic applies automatically
- Descriptive error message when no transcripts found after all fallbacks (instead of cryptic traceback)

## [302] - 2026-02-28

### Fixed
- `tusk session-close` now emits a warning and continues when `tusk-session-stats.py` exits non-zero (e.g. no JSONL transcripts), instead of propagating failure and blocking `tusk merge`
- `tusk commit` now resolves the `tusk` binary via its own path (like `tusk-merge.py`), fixing `FileNotFoundError` when `tusk` is not on PATH

## [301] - 2026-02-28

### Fixed
- `tusk commit` now handles deleted files correctly — files absent from disk are staged via `git rm` instead of `git add`, allowing mixed commits with both new/modified and deleted files

## [300] - 2026-02-28

### Fixed
- `/review-commits` now commits any outstanding working-tree changes (review fixes) before returning, preventing `tusk merge` from failing due to a dirty tree

## [299] - 2026-02-28

### Changed
- `/chain` agents now run `/review-commits` (respecting `review.mode` config) before merging — CHANGES REMAINING stops the agent so the chain orchestrator can surface unresolved findings
- Retro skills (`/retro` lightweight, `/retro` full, `/chain` post-chain) no longer hardcode `--task-type "chore"` for lint rule tasks — agent now picks the closest match from the project's configured `task_types`, or omits the flag if none fits

## [298] - 2026-02-28

- Feature: Add `/investigate` skill — scopes a problem via Plan Mode using read-only tools, then proposes remediation tasks for handoff to `/create-task`. Never writes files or implements anything.

## [297] - 2026-02-27

- Fix: `/tusk` skill step 1 now explicitly says `session_id` is passed to `tusk merge` in step 12, not to `tusk task-done`. SUBCOMMANDS.md adds a warning that `task-done` is for manual out-of-workflow use only — never use it instead of `tusk merge` in the normal dev workflow.

## [296] - 2026-02-27

- Feature: `tusk upgrade` now automatically stages manifest-tracked files and creates a git commit (`Upgrade tusk to vN`) after a successful upgrade. A `--no-commit` flag skips this step for scripting/CI contexts. If the working tree is not a git repo or there are no changed files, upgrade completes normally with a warning.

## [295] - 2026-02-26

- Fix: `tusk upgrade` now also removes the legacy `tusk/tusk.db` file if present (superseded by `tasks.db`).

## [294] - 2026-02-26

- Fix: `tusk upgrade` now deletes deprecated `conventions.md` and `dashboard.html` from target projects. Deletion runs after `tusk migrate` so migration 28→29 can import `conventions.md` into the DB before it is removed. `tusk init` no longer creates `conventions.md`. `tusk conventions --path` now emits a deprecation warning and exits 1.

## [293] - 2026-02-26

- Feature: Dashboard criterion tool timeline now renders a visual swimlane bar above the detail table — each tool call is a color-coded horizontal segment (width ∝ cost) with a hover tooltip showing tool name, cost, and relative time. Tool name dots also added to the table rows for quick color-matching.

## [292] - 2026-02-26

- Fix: Move `VERSION` stamp to the end of `tusk upgrade` so an interrupted upgrade doesn't leave skills, scripts, hooks, or migrations in a stale state while the version check falsely reports "Already up to date".

## [291] - 2026-02-26

- Refactor: Consolidate the dist-excluded script list to a single source of truth (`bin/dist-excluded.txt`); `tusk-generate-manifest.py`, `tusk-lint.py` rule18, and both sites in `install.sh` now read from that file instead of each defining the set inline.

## [290] - 2026-02-26

- Fix: When a bun or pnpm lockfile is detected during `tusk-init` and `tusk-update` test-command detection, inspect `package.json` for vitest/jest before falling back to generic `bun test`/`pnpm test`; suggest `bun run vitest`, `bun run jest`, `pnpm vitest`, or `pnpm jest` as appropriate.

## [289] - 2026-02-26

- Fix: Add early-return guard to `cmd_skill_run` in `tusk-call-breakdown.py` when `_aggregate_single_window` returns empty stats, matching the existing behavior in `cmd_session`.

## [288] - 2026-02-26

- Feature: Add `skill_run_id` FK column to `tool_call_events` via migration 34 (table recreation); update CHECK constraint to `session_id IS NOT NULL OR criterion_id IS NOT NULL OR skill_run_id IS NOT NULL`; `cmd_skill_run` in `tusk-call-breakdown.py` now writes individual event rows alongside aggregated stats.

## [287] - 2026-02-26

- Feature: Add `qualitative` boolean column (default 0) to `conventions` table via migration 33; `tusk conventions add --qualitative` sets it to 1; `tusk conventions list` displays `[qualitative]` tag for qualitative conventions.

## [286] - 2026-02-26

- Feature: `tusk conventions add --qualitative` suppresses the deprecation warning for qualitative (non-grep-detectable) conventions; the warning is retained for plain `conventions add` to continue steering users toward `lint-rule add`.

## [285] - 2026-02-26

- Refactor: `upsert_session_stats` gains a `commit=False` parameter (matching `insert_session_events`); `cmd_task` now batches both writes into a single `conn.commit()` per session for atomic stats updates.

## [284] - 2026-02-26

- Docs: `/create-task` skill now explicitly requires a DOMAIN.md criterion for any task in the `schema` domain or that creates a new DB table, preventing the gap from being caught only at review time.

## [283] - 2026-02-26

- Feature: Dashboard criterion panels now show a vertical tool call timeline (sequence #, tool name, cost, relative timestamp) when per-call event data exists in `tool_call_events`. Falls back to the existing aggregated bar display when no event-level data is available.

## [282] - 2026-02-26

- Refactor: `tusk-call-breakdown.py` eliminates duplicated accumulation loop between `aggregate_tool_calls` and `_aggregate_single_window`. `aggregate_tool_calls` gains an optional `out_items` keyword argument; `_aggregate_single_window` is now a 3-line wrapper that delegates to it.

## [281] - 2026-02-26

- Refactor: `tusk-call-breakdown.py` adds `_aggregate_single_window` helper that reads each transcript once, collecting both stats and items in a single pass. `cmd_session` and `cmd_criterion` now use it instead of calling `aggregate_tool_calls` + `collect_tool_call_items` separately, halving transcript I/O for per-session and per-criterion breakdown calls.

## [280] - 2026-02-26

- Feature: `tool_call_events` table (migration 32) records one row per individual tool call with `task_id`, `session_id`, `criterion_id`, `tool_name`, `cost_dollars`, `tokens_in`, `tokens_out`, `call_sequence`, and `called_at` timestamp. `tusk-call-breakdown.py` populates it alongside the existing `tool_call_stats` aggregates for sessions and criteria.

## [279] - 2026-02-26

- Fix: `tusk-generate-manifest.py` is no longer distributed to target projects. `install.sh` skips it via an exclusion list; `MANIFEST`, `rule18_manifest_drift`, and `build_manifest()` all apply the same exclusion so Rule 18 remains passing.

## [278] - 2026-02-26

- Feature: `tusk generate-manifest` command regenerates the `MANIFEST` file from the current source tree using the same logic as `install.sh` section 4c. Makes MANIFEST drift trivially fixable — run `tusk generate-manifest`, review the diff, commit.

## [277] - 2026-02-26

- Feature: `tusk lint` Rule 18 detects MANIFEST drift — reports blocking violations when the committed `MANIFEST` file is missing entries present in the source tree (`bin/tusk-*.py`, `skills/*/`, `.claude/hooks/*`) or contains entries no longer distributed by `install.sh`.

## [276] - 2026-02-25

- Feature: `tusk upgrade` now uses a two-manifest approach to safely remove orphaned files. `install.sh` writes `.claude/tusk-manifest.json` after install; `tusk upgrade` diffs the stored manifest against the new release's `MANIFEST` file and `rm`s any orphan skills, scripts, or hooks — leaving user-added files untouched. A `MANIFEST` file is now included in each release tarball.

## [275] - 2026-02-25

- Fix: `tusk-init` Step 2d now routes fresh projects (no codebase signals) to a new **Step 2e** interview that asks three targeted questions (project type, tech stack, expected work areas) and maps answers to concrete domain/agent suggestions feeding directly into Steps 3 and 4. Edge Cases note updated to call out Step 9 as the primary seeding path for fresh projects.

## [274] - 2026-02-25

- Docs: `/loop` and `/groom-backlog` SKILL.md files now include a Headless / CI Usage section documenting `--on-failure` flag usage, non-interactive assumptions, and example CI invocations.

## [273] - 2026-02-25

- Fix: `tusk loop --dry-run --on-failure <strategy>` now includes `--on-failure <strategy>` in the printed dispatch line when skill is `chain`.

## [272] - 2026-02-25

- Feature: `/chain` accepts `--on-failure skip|abort` for unattended execution — applies the strategy automatically when an agent finishes without completing, instead of prompting interactively. `skip` logs a warning and continues to the next wave; `abort` stops the chain immediately. Default (no flag) is unchanged.
- Feature: `tusk loop --on-failure skip|abort` passes the flag through to each `/chain` dispatch for fully unattended overnight runs.

## [271] - 2026-02-25

- Fix: `tusk branch` stash-pop message on `git pull` failure (and other post-checkout failure paths) now appends the current branch name and a reminder to switch back to the original branch.

## [270] - 2026-02-25

- Fix: `tusk branch` now pops the auto-stash and notifies the user on existing-branch and new-branch `git checkout` failure paths, covering the two remaining stash-leak cases.

## [269] - 2026-02-26

- Fix: `tusk merge` no longer blocks on untracked files; dirty-check now uses `git diff --quiet` (unstaged) and `git diff --cached --quiet` (staged) instead of `git status --porcelain`, so only actual changes to tracked files prevent a merge.

## [268] - 2026-02-25

- Fix: `tusk branch` now pops the auto-stash and notifies the user on `git checkout` and `git pull` failure paths, matching the existing behaviour on the multiple-branches error path.

## [267] - 2026-02-25

- Warn and stash uncommitted changes in tusk branch; leave stash intact so changes are not silently carried into new feature branch

## [266] - 2026-02-25

- Redesign criterion tool cost panel: styled card with rotating chevron arrow, total cost right-aligned, proper CSS classes replacing inline styles

## [265] - 2026-02-25

- Fix criterion-item layout: switch to column flex so tool cost dropdown renders below row content instead of inline

## [264] - 2026-02-25

- Add Tool Cost Breakdown dropdown to each acceptance criterion, proportionally calculated from task-level tool stats by criterion cost fraction

## [263] - 2026-02-25

- Remove completion timestamp from criteria items; move cost pill inline between status and text

## [262] - 2026-02-25

- Remove token count pill from acceptance criteria group headers

## [261] - 2026-02-25

- Remove Manual type and Original source pills from acceptance criteria rows

## [260] - 2026-02-25

- Remove By Commit/By Status/Flat view toggles from criteria detail rows; always group by commit

## [259] - 2026-02-25

- Remove Domain, Sessions, and Updated columns from task table; default sort now by Cost

## [258] - 2026-02-25

- Remove Domain and Type filter dropdowns from task table filter bar

## [257] - 2026-02-25

- Replace status filter pill buttons (All/To Do/In Progress/Done) with a Status dropdown consistent with Domain, Size, and Type filters

## [256] - 2026-02-25

- Remove KPI cards and dead complexity section from Tasks tab; Tasks tab now shows only the filter bar + table + pagination

## [255] - 2026-02-25

- Remove Velocity chart and all associated code from dashboard (generate_velocity_section, fetch_velocity call, velocity_html template variable)

## [254] - 2026-02-25

- Redesign Cost Trend panel: replace Tasks/Skills type toggle with always-visible dual chart sections (Tasks + Skills), each showing period-controlled bar+cumulative-line charts; single clearly-labeled Period toggle (Daily/Weekly/Monthly) controls both; Skills section appends an all-time By Skill breakdown

## [253] - 2026-02-25

- Remove Project-Wide Tool Costs section from dashboard Skills tab

## [252] - 2026-02-25

### Changed
- `tusk-dashboard.py`: prefix dashboard output filename with the repo root directory name (e.g., `myproject-dashboard.html`) so multiple projects can have dashboards open simultaneously without overwriting each other

## [251] - 2026-02-24

### Changed
- `config.default.json`: replaced two specialized reviewers (`security`, `correctness`) with a single `general` reviewer covering all seven code review dimensions (correctness, security, readability & maintainability, design, tests, performance, operational concerns)
- `skills/review-commits/REVIEWER-PROMPT.md`: updated Step 2 from a prose list to an explicit numbered checklist of the same seven dimensions

## [250] - 2026-02-24

### Changed
- `/chain` skill: added convention note encouraging `/create-task` as the default for all task creation over raw `bin/tusk task-insert`

## [249] - 2026-02-24

### Changed
- `/loop` skill: added convention note encouraging `/create-task` as the default for all task creation over raw `bin/tusk task-insert`

## [248] - 2026-02-24

### Changed
- `/groom-backlog` skill: added convention note encouraging `/create-task` as the default for all task creation over raw `bin/tusk task-insert`

## [247] - 2026-02-24

### Changed
- `/retro` and `/review-commits` skills: added convention note encouraging `/create-task` as the default for all task creation over raw `bin/tusk task-insert`

## [246] - 2026-02-24

### Changed
- `/tusk` skill: added convention note at the top encouraging `/create-task` as the default for all task creation over raw `bin/tusk task-insert`

## [245] - 2026-02-25

### Changed
- [TASK-396] Renamed `/reconfigure` skill to `/tusk-update` — moved `skills/reconfigure/` to `skills/tusk-update/`, updated frontmatter `name` field, regenerated symlink via `tusk sync-skills`, and updated all references in `CLAUDE.md`

## [244] - 2026-02-25

### Changed
- [TASK-395] `/chain` wave dispatch now includes `isolation: worktree` for parallel agents, preventing file conflicts and branch-state interference between concurrent agents

## [243] - 2026-02-24
- `bin/tusk-pricing-lib.py`: added `commit=True` parameter to `upsert_criterion_tool_stats` so callers can defer the commit and batch additional writes into one atomic transaction
- `bin/tusk-call-breakdown.py`: `--criterion` recompute now calls `upsert_criterion_stats` with `commit=False` and issues a single `conn.commit()` after the `acceptance_criteria` cost UPDATE, making both writes atomic; added clarifying comments

## [242] - 2026-02-24
- `bin/tusk-call-breakdown.py`: `--criterion` recompute now refreshes `acceptance_criteria.cost_dollars`, `tokens_in`, and `tokens_out` after updating `tool_call_stats`, keeping AC cost summary in sync with recomputed tool stats; shared-commit groups update all members with the split values

## [241] - 2026-02-24
- `bin/tusk-merge.py`: move working-tree cleanliness check and branch detection before session close, so a dirty-tree or missing-branch failure leaves both session and task untouched

## [240] - 2026-02-24
- `skills/retro/SKILL.md`: added FOCUS.md check to LR-1; custom categories replace A–D when file present; added Customization section documenting the hook
- `skills/retro/FULL-RETRO.md`: added FOCUS.md check to Steps 1 and 3
- `skills/retro/FOCUS.md.example`: new template showing default category format for user customization

## [239] - 2026-02-24
- `tusk-setup.py`: removed `conventions` key from output (retro now creates lint-rule tasks instead of writing conventions)
- `skills/retro/SKILL.md`, `FULL-RETRO.md`: replaced Category D conventions with lint-rule task creation; `/retro` now files tasks with `tusk lint-rule add` invocations instead of calling `tusk conventions add`
- `skills/create-task/SKILL.md`: removed conventions preamble context from Step 2
- `bin/tusk`: `conventions add` now emits a deprecation warning

## [238] - 2026-02-24
- `bin/tusk`: added `lint_rules` table to `cmd_init`; migration 30→31 adds the table to existing DBs; new `lint-rule` dispatcher entry
- `bin/tusk-lint-rules.py`: new script implementing `tusk lint-rule add/list/remove` commands
- `bin/tusk-lint.py`: added Rule 16 (DB-backed blocking rules) and Rule 17 (DB-backed advisory rules) that query `lint_rules` and run each as a `grep -E` check
- `DOMAIN.md`: documented `lint_rules` entity

## [237] - 2026-02-24
- `skills/reconfigure/SKILL.md`: added `test_command` to the configurable fields table; new Step 2b shows current value, auto-detects test framework (package.json → `npm test`, pyproject.toml/setup.py → `pytest`, Cargo.toml → `cargo test`, Makefile with test target → `make test`), and prompts user to confirm, use detected, override, or clear

## [236] - 2026-02-24
- `skills/tusk-init/SKILL.md`: Step 2a now scans for Makefile; new Step 5b auto-detects test command from project manifests (package.json → `npm test`, pyproject.toml/setup.py → `pytest`, Cargo.toml → `cargo test`, Makefile with test target → `make test`) and prompts user to confirm, override, or skip; Step 6 config template now includes `test_command`

## [235] - 2026-02-24
- `skills/tusk/SKILL.md`: step 7 now documents that `tusk commit` hard-fails (exit code 2) when `test_command` is set and tests fail; instructs agent to read full test output, trace root cause, implement fix, and retry — with a 3-attempt max-retries guardrail before surfacing failure to the user

## [234] - 2026-02-24
- `bin/tusk-commit.py`: use distinct exit codes — `2` = test_command failed, `3` = git add/commit failed, `4` = criteria marking failed (was all `2` except criteria which was `3`); document exit codes in module docstring

## [233] - 2026-02-24
- `config.default.json`: added `test_command` top-level key (empty string default = disabled)
- `bin/tusk`: added `test_command` to `KNOWN_KEYS` in `validate_config`; added validation block ensuring `test_command` is a string when present
- `bin/tusk`: `commit` dispatcher now passes config path to `tusk-commit.py`
- `bin/tusk-commit.py`: added Step 2 test gate — reads `test_command` from config, runs it after lint and before staging when non-empty; non-zero exit hard-blocks the commit with full streamed output; `--skip-verify` bypasses the gate; empty/null/unset silently skips

## [232] - 2026-02-24
- `bin/tusk-call-breakdown.py`: `--criterion` recompute now detects shared-commit groups (N criteria with the same `commit_hash`), adjusts the window start to exclude group members, uses the latest `completed_at` as window end, splits stats evenly across N, and upserts into `tool_call_stats` for all group members — mirroring the `capture_criterion_cost` logic in `tusk-criteria.py`

## [231] - 2026-02-24
- `bin/tusk-lint.py`: added Rule 15 (advisory) — warns when all completed non-deferred criteria for an In Progress task share a single commit hash (big-bang commit pattern); only fires when 2+ eligible criteria exist and all share one hash

## [230] - 2026-02-24
- `config.default.json`: `review.reviewers` changed from flat string array to array of objects with `name` and `description` fields; ships 2 default personas: `security` (injection, auth bypass, data exposure, input validation, secrets, unsafe deserialization) and `correctness` (logic errors, edge cases, missing error handling, race conditions, contradicts acceptance criteria)
- `REVIEWER-PROMPT.md`: added `{reviewer_focus}` placeholder populated with the reviewer's description
- `/review-commits` SKILL.md Step 5: documents `{reviewer_focus}` placeholder and fallback text for unassigned reviews
- `bin/tusk`: updated `review.reviewers` validation to require objects with `name` (string) and `description` (string) fields
- `bin/tusk-review.py`: `cmd_start` normalizes reviewer items — extracts `name` from objects, passes strings/None through unchanged

## [229] - 2026-02-24
- Schema migration 30: dropped `pending` from `review_comments.resolution`; NULL is now the unresolved sentinel, with CHECK restricted to `fixed | deferred | dismissed`
- `tusk-review.py`: INSERT explicitly sets `resolution = NULL`; open/resolved split uses `IS NULL` / `IS NOT NULL` instead of `= 'pending'`
- `DOMAIN.md`: documented Review Comment resolution semantics (fixed=immediate, deferred=creates task, dismissed=skipped)

## [228] - 2026-02-24
- `tusk criteria done` / `tusk commit --criteria`: split cost evenly across all N criteria that share a commit hash; tool_call_stats receive proportional attribution; single-criterion behavior unchanged

## [227] - 2026-02-24
- `tusk commit`: suppress redundant shared-commit warnings for criteria[1:] in multi-criteria batches via new `--batch` flag on `tusk criteria done`

## [226] - 2026-02-24
- `tusk merge`: when no open session exists, fall back to the most recently closed session for the task (emits a warning); only error when no sessions exist at all

## [225] - 2026-02-24
- `tusk merge`: detect dirty working tree before `git pull` and abort with a clear error message instructing the user to stash or commit changes first

## [224] - 2026-02-24
- `tusk merge`: treat "Session N is already closed" as a non-fatal warning so retrying after an interrupted merge no longer aborts before any merge work happens

## [223] - 2026-02-24
- Updated `/tusk` SKILL.md Step 7 to use repeatable-flag form (`--criteria <id>` per criterion), explicitly state one-commit-per-criterion as the default, and add grouping guidance (2–3 co-located criteria acceptable with rationale; never bundle all on one end-of-task commit)
- Updated `CLAUDE.md` and `tusk-commit.py` usage strings to match the new repeatable-flag notation

## [222] - 2026-02-24
- Surface shared-commit warning in `tusk commit`: removed `--allow-shared-commit` from automated `criteria done` calls so the warning fires by default
- Strengthened shared-commit warning in `tusk-criteria.py` to distinguish intentional co-located grouping (acceptable) from big-bang commits (problematic)

## [221] - 2026-02-23

### Fixed
- `tusk token-audit`: redundancy detector no longer false-positives on `tusk conventions add/reset` after `tusk setup`; only bare `tusk conventions` and `tusk conventions list` are flagged as redundant re-fetches

## [220] - 2026-02-23

### Changed
- `/check-dupes` skill: removed "Callers" integration-points paragraph (prose-only, no functional impact)
- `/lint-conventions` skill: removed "Interpreting Results" section (self-evident from command output)
- `/tusk` skill: removed "Important Guidelines" boilerplate section

## [219] - 2026-02-23

### Changed
- `tusk commit --criteria` now accepts multiple space-separated IDs per flag (`--criteria 1042 1043 1044`); single-ID usage unchanged

## [218] - 2026-02-23

### Added
- `tusk chain scope|frontier|status` now accept multiple head task IDs for parallel-root DAG execution; union BFS merges sub-DAGs with minimum depth; validation rejects multi-head calls where heads share no common downstream task
- `/chain` skill updated to run multiple heads as parallel wave 0 and use union frontier for subsequent waves

## [217] - 2026-02-23

### Fixed
- `tusk branch`: suppress false "merge conflicts" error when the working tree has only untracked files by treating tracked-file dirtiness separately from untracked-file presence

## [216] - 2026-02-23

### Changed
- `tusk lint` Rule 1: now also scans `bin/tusk-*.py` for subprocess-based sqlite3 CLI calls (still allows Python's native `sqlite3` module for read-only queries in bin scripts)

## [215] - 2026-02-23

### Changed
- `tusk merge`: `--session` is now optional; when omitted, the single open session for the task is auto-detected (errors clearly if none or multiple open sessions exist)

## [214] - 2026-02-23

### Added
- `tusk merge <task_id> --session <session_id>`: new command that finalizes a task in one call — closes the session (capturing diff stats), ff-only merges the feature branch into the default branch, pushes, deletes the branch, and calls `tusk task-done --reason completed --force`; `--pr --pr-number <N>` flag (or `merge.mode = pr` in config) uses `gh pr merge --squash --delete-branch` instead of local merge
- `merge.mode` config key added to `config.default.json` (values: `local` | `pr`, default: `local`) with validation in `tusk validate`

## [213] - 2026-02-23

### Added
- Restored `tusk branch <task_id> <slug>` command: creates a feature branch off the detected default branch, auto-stashes dirty working trees, and warns when an existing branch for the task is found

## [212] - 2026-02-23

### Changed
- `tusk-dashboard-html.py`: Global Tool Costs table now includes a `Tokens In` column alongside `Total Calls`, `Total Cost`, and `Share of Total`

## [211] - 2026-02-23

### Added
- `conventions` DB table (id, text, source_skill, lint_rule, violation_count, created_at) — replaces append-only `tusk/conventions.md` as source of truth; schema migration 28→29 imports existing file entries
- `tusk conventions add "<text>" [--source <skill>]` — inserts a new convention row into the DB
- `tusk conventions` (no args) — now reads from DB instead of printing the flat file; reconstructs markdown format per row

### Changed
- `tusk-setup.py`: `conventions` field in `tusk setup` JSON now sourced from DB conventions table (falls back to `conventions.md` for installs without the table)
- `/retro` skill (SKILL.md and FULL-RETRO.md): convention writing now uses `tusk conventions add` instead of appending to `tusk/conventions.md`

## [210] - 2026-02-23

### Fixed
- `tusk task-update --no-deferred`: now also clears `expires_at` so autoclose no longer expires a task after it has been un-deferred
- `tusk task-update`: passing both `--deferred` and `--no-deferred` now errors with "mutually exclusive" instead of silently applying the last flag

### Changed
- `tusk task-update`: tightened `updates` type annotation from bare `dict` to `dict[str, Any]`

## [209] - 2026-02-23

### Added
- `tusk task-update --deferred`: sets `is_deferred=1`, prefixes summary with `[Deferred]`, sets `expires_at` to +60 days if unset
- `tusk task-update --no-deferred`: sets `is_deferred=0`, strips `[Deferred]` prefix from summary

## [208] - 2026-02-23

### Added
- Lint Rule 14 (advisory): detects open tasks where `is_deferred` flag and `[Deferred]` summary prefix disagree

## [207] - 2026-02-23

### Added
- `is_deferred INTEGER NOT NULL DEFAULT 0 CHECK (is_deferred IN (0, 1))` column on `tasks` table (schema migration 28)

### Changed
- WSJF formula uses `is_deferred = 0` instead of `summary NOT LIKE '%[Deferred]%'` for the non-deferred bonus
- `tusk task-insert --deferred` and manual `[Deferred]` prefix both set `is_deferred = 1` on insert
- `tusk autoclose` queries `is_deferred = 1` instead of `LIKE '%[Deferred]%'`
- Lint Rule 9 checks `is_deferred = 1` instead of `LIKE '[Deferred]%'`

## [206] - 2026-02-23

### Added
- `fetch_velocity()` in `tusk-dashboard-data.py` consuming the `v_velocity` view (tasks/week with avg cost)
- Velocity card panel in the dashboard Tasks tab showing a weekly table and bar chart
- Step 4 Velocity Summary in `/tusk-insights` audit (tasks/week for last 8 weeks)

## [205] - 2026-02-23

### Added
- Partial UNIQUE index on `task_sessions(task_id) WHERE ended_at IS NULL` to prevent duplicate open sessions (schema migration 27)
- `tusk task-start` detects concurrent session race via `IntegrityError` and reuses the winning session with a warning

## [204] - 2026-02-23

### Added
- `agent_name TEXT` nullable column on `task_sessions` and `code_reviews` (schema migration 26)
- `--agent <name>` flag on `tusk task-start` to record which named agent opened the session
- `--agent <name>` flag on `tusk review start` to record which named agent ran the review

## [203] - 2026-02-23

### Changed
- Rewrote `/tusk` skill to branch-free commit workflow: removed mandatory branch creation step, inlined review-commits + task-done + retro as steps 10–11, gutted `FINALIZE.md` to a supersession stub

## [202] - 2026-02-23

### Changed
- Removed `ai_then_human` from valid `review.mode` values; `tusk validate` now emits a targeted deprecation error pointing to `ai_only`
- Updated `skills/tusk/FINALIZE.md` to replace `ai_then_human` section with a deprecation fallback note (falls back to `ai_only` behavior)
- Updated `CLAUDE.md` to list valid `review.mode` values (`disabled`, `ai_only`)

## [201] - 2026-02-23

### Changed
- Renamed `/review-pr` skill to `/review-commits`; diff source now uses `git diff <base>...HEAD` instead of `gh pr diff`
- Updated `skills/tusk/FINALIZE.md` and `skills/check-dupes/SKILL.md` to reference `/review-commits`
- Updated `CLAUDE.md` to document the new skill name

### Removed
- `skills/review-pr/` directory replaced by `skills/review-commits/`

## [200] - 2026-02-23

### Removed
- `github_pr` column dropped from `tasks` table (schema migration 24→25)
- Removed `--github-pr` flag from `tusk task-update`
- Removed merged-PR auto-close check from `tusk autoclose` (no longer has a column to query)
- Removed `github_pr` references from `tusk-dashboard-data.py`, `tusk-dashboard.py`, `skills/chain/SKILL.md`, `skills/tusk/SUBCOMMANDS.md`, and `skills/review-pr/SKILL.md`

## [199] - 2026-02-23

### Changed
- `tusk task-done` step 2b now prints `Error:` when blocking (force=False) and `Warning:` when overriding (force=True), matching the established convention in step 2

## [198] - 2026-02-23

### Changed
- `tusk task-done` commit-hash check now only applies when `--reason completed`; `wont_do`, `duplicate`, and `expired` closes skip the check; error message now hints at `--force` for non-git environments and pre-migration rows
- Update CLAUDE.md to document the new step 2b commit-hash check in `tusk-task-done.py`

## [197] - 2026-02-23

### Changed
- `tusk task-done` now warns and exits non-zero if any completed acceptance criterion has no `commit_hash`; listing the offending criteria; `--force` overrides but still emits the warning

## [196] - 2026-02-23

### Added
- `v_velocity` view: completed tasks grouped by calendar week (Mon-start, `%Y-W%W`) with task_count, avg_cost, avg_tokens_in, avg_tokens_out (schema migration 24)

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
