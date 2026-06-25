# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

tusker is a portable task management system for Claude Code projects. It provides a local SQLite database, a bash CLI (`bin/tusk`), Python utility scripts, and Claude Code skills to track, prioritize, and work through tasks autonomously.

When proposing, evaluating, or reviewing features, consult `docs/PILLARS.md` for design tradeoffs. The pillars define what tusk values and provide a shared vocabulary for resolving competing approaches.

## Context Handoff Model

Tusk is a durable context snapshot system. Treat objectives as larger intent units, tasks as shippable units, criteria as completion units, verifications as proof units, and context atoms as memory units captured in task descriptions, criteria, progress, review comments, jots, and retro findings.

The write side (`/create-task`, `/retro`, `/investigate`, and direct `task-insert`/`criteria add` calls) should leave enough structured context for a future `/tusk` run to act without the original chat. The read side (`/tusk`, `/resume-task`, `/chain`) should start from `task-get`, criteria, dependencies, and progress, then load only the task-relevant context needed to ship the next change.

## Commands

```bash
# Init / info
bin/tusk init [--force]
bin/tusk path
bin/tusk config [key]
bin/tusk setup          # config + backlog + conventions in one JSON call
bin/tusk validate
bin/tusk skill-drift [--format text|json] [--advisory]   # report tusk subcommands referenced by installed skills but absent from this CLI (version skew → run tusk upgrade); also run advisory by tusk validate (issue #1035) and proactively at tusk task-start via --advisory, gated TTY/quiet so agent/CI callers stay silent (issue #1122)

# Task lifecycle
bin/tusk task-get <task_id>        # accepts integer ID or TASK-NNN prefix form
bin/tusk task-list [--status <s>] [--domain <d>] [--assignee <a>] [--workflow <w>] [--objective <id>] [--format text|json] [--all] [--include-shadows] [--bakeoff <id>]  # list tasks (not the built-in TaskList tool); bakeoff shadows are hidden by default; --objective filters to tasks linked to that objective
bin/tusk task-select [--max-complexity XS|S|M|L|XL]
bin/tusk task-insert "<summary>" "<description>" [--priority P] [--domain D] [--task-type T] [--assignee A] [--complexity C] [--workflow W] [--criteria "..." ...] [--typed-criteria '{"text":"...","type":"...","spec":"..."}' ...] [--expires-in DAYS] [--fixes-task-id ID] [--skip-dupe]
# --skip-dupe (alias --force) bypasses the internal fuzzy duplicate guard and inserts unconditionally (issue #1127) — for batch callers that already dedup on a stable identity key (e.g. one task per venue keyed on google_place_id) where fuzzy summary matching mislinks distinct items that share category nouns. Without it, a fuzzy match still prints {"duplicate":true,...} and exits 1.
bin/tusk task-start [<task_id>] [--force] [--force-deps] [--force-contingent] [--force-session] [--agent NAME] [--skill NAME]   # --skill opens a skill_runs row attributed to the task (returned under result.skill_run); --force-session intentionally attaches to an existing active session from outside the task workspace
bin/tusk task-done <task_id> --reason completed|expired|wont_do|duplicate [--force]
bin/tusk task-update <task_id> [--priority P] [--domain D] [--task-type T] [--assignee A] [--complexity C] [--workflow W] [--summary S] [--description D] [--not-before TIMESTAMP]
bin/tusk task-reopen <task_id> --force
bin/tusk task-unstart <task_id> --force [--close-sessions]   # revert a cleanly-orphaned In Progress task back to To Do; refuses if it has any task_progress rows, [TASK-N] commits whose diff overlaps with files referenced by the task, or an open session. --close-sessions (issue #1043) closes the task's open session(s) instead of refusing on them — the one-command skip path for a just-started task; it does NOT bypass the progress-checkpoint or commit-overlap guards. Historical [TASK-N] commits whose diff has no overlap with task scope (e.g. left over from a prior task numbering, issue #627) are treated as prefix-match false positives and ignored — same aggregate file-overlap heuristic as `tusk check-deliverables`' `merged_not_closed_low_confidence` (unions every matched commit's files into one set and asks "is the whole batch off-scope?" — appropriate for a refuse-or-proceed binary decision). `tusk task-summary` uses a related-but-distinct **block-level** variant (issue #663): commits are first grouped into connected components on the parent chain, then each block is kept or dropped on its own scope-signal overlap, so legitimate sibling commits (VERSION bumps, CHANGELOG, new test files, brand-new feature files) ride along on the back of an in-block commit that names a referenced path. The scope-signal overlap is checked at two levels (issue #670): full-path equality between block files and `task_referenced_paths`, OR basename equality between block-file basenames and `task_referenced_basenames` — the latter resolves descriptions that name a touched file by bare basename (e.g. `FULL-RETRO.md`) against commits touching `skills/retro/FULL-RETRO.md`. Tasks whose description and criteria reference no paths AND no bare basenames have no scope signal and the original refusal stands.
bin/tusk task-brief <task_id> [--format json|markdown]   # compiled pickup brief for context hydration; read-only and does not replace task-start/session state
bin/tusk task-summary <task_id> [--format json|markdown]   # end-of-run rollup: identity, cost, duration, diff, criteria counts (JSON default; markdown for user-facing display)
bin/tusk task-worktree create <task_id> <slug> [--workspace-root <path>]  # create or reuse a task-owned git worktree and emit JSON
bin/tusk task-worktree list [--format json]   # list recorded task worktrees reconciled with git worktree list
bin/tusk task-worktree prune [--dry-run] [--format json]   # delete stale task workspace registry rows that no longer exist on disk or in git worktree list

# Dev workflow
bin/tusk branch <task_id> <slug>
bin/tusk commit <task_id> "<message>" "<file1>" ["<file2>" ...] [--criteria <id>] ... [--skip-verify]
bin/tusk commit <task_id> "<file1>" ["<file2>" ...] -m "<message>" [--criteria <id>] ... [--skip-verify]
# Note: tusk commit prepends [TASK-N] to <message> automatically; duplicate [TASK-N] prefixes are stripped
# Note: bare -- separators are silently ignored (AI callers sometimes insert them)
# Note: always quote file paths — zsh expands unquoted [brackets] as glob patterns before tusk receives them
# Note: tusk commit, task-insert, task-update, criteria add, progress (--note/--next-steps), context add (--content), jot (category + note args), and review (add-comment text + --note on resolve/approve/request-changes) all reject shell-substitution metacharacters (backtick, $(...), ${...}, bare $IDENT) in their text args via the shared reject_shell_metacharacters guard in bin/tusk-git-helpers.py (issue #881 for commit messages; issue #1106 extended it to task summary/description and criterion text; issue #1107 extended it to progress/context/jot-note/review; issue #1108 closed the jot category positional and audited the remaining operator-authored surfaces). The guard rejects rather than auto-escapes because zsh/bash expand those patterns before tusk sees the argv, even inside double quotes. task-insert's --description-file is the immune path for untrusted text; typed-criteria/file specs are NOT checked (shell code by design). The operator-authored DB-write surfaces — tusk conventions add/update, glossary set-definition/add, and lint-rule add/update message — are intentionally EXEMPT (documented, not guarded): they are operator-authored, low-frequency, and legitimately contain literal shell-syntax examples (they document shell hazards), so guarding would block their primary use case and there is no agent-relay corruption vector. gh issue/pr comment calls (in /address-issue and /review-commits) are external tools tusk does not wrap, so they remain unguarded — manual care still required.
bin/tusk merge <task_id> [--session <session_id>] [--pr --pr-number <N>] [--rebase] [--skip-lint] [--skip-verify] [--allow-diverged-default]
bin/tusk progress <task_id> [--note "..."] [--next-steps "..."]
bin/tusk jot <category> "<note>" [--file <path>] [--skill <name>]   # capture mid-task friction at the source — keyed to active skill_run; consumed by /retro
bin/tusk jots [--skill-run-id <id>] [--task-id <id>] [--limit N]    # list jots filtered by skill_run_id and/or task_id (newest-first JSON array)
bin/tusk bakeoff <task_id> --models m1,m2[,mN] [--workspace-root <path>] [--claude-bin <path>] [--dry-run]  # run the same task under N models in parallel worktrees and emit a side-by-side report
bin/tusk bakeoff pick <bakeoff_id> <shadow_id> [--rebase]   # merge the chosen shadow's branch into the source task's base branch, close the source session, mark source Done (completed), and delete sibling shadow rows + worktrees. --rebase mirrors `tusk merge --rebase`: rebase chosen shadow onto default before the ff-only merge when the default branch has advanced during the bakeoff
bin/tusk bakeoff discard <bakeoff_id>            # throw every shadow for this bakeoff away — delete shadow rows + force-remove worktrees; source task left untouched

# Criteria
bin/tusk criteria add <task_id> "criterion" [--source original|subsumption|pr_review] [--type manual|code|test|file] [--spec "..."]
bin/tusk criteria list <task_id>
bin/tusk criteria done <criterion_id> [--skip-verify]
bin/tusk criteria skip <criterion_id> --reason <reason>
bin/tusk criteria reset <criterion_id>
bin/tusk criteria delete <criterion_id> [--force]  # --force allows deleting completed criteria

# Context atoms
bin/tusk context add <task_id> --type memory|assumption|question|risk|decision|entry_point --content "..." [--source manual|create_task|task_progress|review|retro|agent_handoff] [--objective-id ID]
bin/tusk context list <task_id> [--type memory|assumption|question|risk|decision|entry_point] [--status active|resolved|superseded|all] [--format json|text]
bin/tusk context resolve <context_item_id>
bin/tusk context supersede <context_item_id>

# Objectives (initiative-level intent spanning one or more tasks; tasks stay the independent shippable unit)
bin/tusk objective insert "<summary>" [--description <text>]
bin/tusk objective list [--status active|completed|abandoned|all] [--format json|text]
bin/tusk objective get <objective_id> [--format json|text]   # accepts integer ID or OBJ-NNN prefix form; includes linked tasks
bin/tusk objective brief <objective_id> [--format json|markdown]   # read-side rollup over linked tasks: status breakdown, criteria coverage, summed cost/duration (per distinct task — no double-count), open objective-scoped context
bin/tusk objective update <objective_id> [--summary S] [--description D] [--status active|completed|abandoned]
bin/tusk objective link <objective_id> <task_id> [--type primary|contributes_to|follow_up]   # re-linking the same pair upserts relationship_type
bin/tusk objective unlink <objective_id> <task_id>
bin/tusk objective done <objective_id> --reason completed|abandoned   # closes the objective only; never touches linked task status
# Linked objectives also surface in `tusk task-get <id>` output (objectives array) and filter `tusk task-list --objective <id>`

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
bin/tusk retro-patches [--window-days N] [--unconfirmed]   # → [{finding_id, skill_run_id, task_id, action_taken, target_file, created_at, age_days}, ...] — list `skill-patch:<file>` retro_findings; `--unconfirmed` filters to those without a later `skill-patch-confirmed:<file>` row (issue #540)
bin/tusk skill-patch-priority <task_id> [--apply] [--format json|text]   # → compute a priority for a skill-patch follow-up task from its retro-signals (reopen_count + rework_chain.fixes/fixed_by + review_themes recurrence → a "pressure" score; higher pressure climbs the config `priorities` ladder, zero pressure stays at the default rung). --apply persists it onto the task. Wired into /retro (at deferred skill-patch task creation) and /groom-backlog (reprioritization) so skill-patch tasks no longer rot at unmodified default priority (TASK-715)
bin/tusk test-detect               # → {"command": "<cmd>", "confidence": "high|medium|low|none"}
bin/tusk add-lib [--lib <name>] [--repo <owner/repo>] [--ref <branch|tag|sha>]  # → {"lib": "<name>", "tasks": [...], "error": null}
bin/tusk init-fetch-bootstrap      # → {"libs": [{name, repo, tasks, error}, ...]}
bin/tusk init-write-config [--domains <json>] [--agents <json>] [--task-types <json>] [--test-command <cmd>] [--project-type <type>] [--project-libs <json>] [--worktree-symlink-files <json>]  # → {"success": bool, "config_path": "...", "backed_up": bool}
bin/tusk git-default-branch        # → prints default branch name (e.g. "main"); symbolic-ref → gh fallback → "main"
bin/tusk branch-parse [--branch <name>]  # → {"task_id": N}; parses task ID from current or named branch
bin/tusk sql-quote "O'Reilly's book"   # → 'O''Reilly''s book'
bin/tusk shell

# Versioning
bin/tusk version
bin/tusk version-bump                              # increment VERSION by 1, stage, echo new version
bin/tusk changelog-add [--from-version-file] [<version>] [<task_id>...]   # prepend dated entry to CHANGELOG.md; <version> defaults to VERSION-file content and must match if passed explicitly
bin/tusk migrate
bin/tusk regen-triggers
bin/tusk sync-main                                # fetch + ff-only pull of origin/<default> + stash-by-ref + tusk migrate — staleness recovery for /address-issue Step 4.6 when local main is behind origin
bin/tusk dev-sync [--dry-run]                     # source-repo only: copy bin/tusk + bin/tusk-*.py + UNDERSCORE_BIN_FILES into .claude/bin/ and refresh tusk-lint.py.hash. Refuses outside the source repo.
bin/tusk upgrade [--no-commit] [--force]  # --no-commit: skip auto-commit; --force: upgrade even if version matches or exceeds remote
```

Additional subcommands (`blockers`, `review`, `chain`, `loop`, `deps blocked/all`, `session-stats`, `session-close`, `session-recalc`, `skill-run`, `call-breakdown`, `token-audit`, `pricing-update`, `sync-skills`, `reconcile-skills`, `dev-sync`, `dashboard`) follow the same `bin/tusk <cmd> --help` pattern — see source or run `--help` for flags.

There is no build step or external linter in this repository.

## Default Task Workflow

The default isolated unit of work is a task workspace: a task-owned git worktree created with `bin/tusk task-worktree create <task_id> <slug>`. Use it after `task-start` and before implementation. The command creates or reuses a workspace at `$TUSK_WORKTREE_ROOT/TASK-<id>-<slug>` or, when the environment variable is unset, `~/.tusk/worktrees/TASK-<id>-<slug>`. The workspace checks out `feature/TASK-<id>-<slug>`, which remains the version-control handle for commits, review, and merge.

`bin/tusk branch <task_id> <slug>` remains available for compatibility and unusual branch-only flows, but normal task execution should use `task-worktree create` so parallel tasks do not share a checkout. `bin/tusk task-worktree list --format json` is the recovery view for recorded workspaces; it reconciles the database with live `git worktree list` output and surfaces stale rows or missing paths.

`bin/tusk merge <task_id> --session <session_id>` removes the recorded task workspace before deleting the feature branch. If the task workspace is dirty, cleanup refuses so local files are not lost; clean or stash in that workspace and retry. If the local files are intentionally disposable, force-remove the worktree with git and rerun the tusk command so the task workspace registry can be cleaned up.

**Auto-symlink gitignored runtime files (issue #752):** `task-worktree create` reads the `worktree.symlink_files` array from `tusk/config.json`. Entries are partitioned by shape:

- **Bare basenames** (no `/` — e.g. `node_modules`, `.venv`, `.env`) walk the primary repo (skipping `.git`) and create an **absolute-path** symlink at every match — top-level and nested. Right for single-project repos where one canonical location exists per name.
- **Path-style entries** (contain `/` — e.g. `apps/web/node_modules`, `apps/scraper/.venv`, issue #867) are treated as project-relative paths and create exactly one symlink at `<worktree>/<entry>` → `<primary>/<entry>` iff the primary target exists. Right for monorepos where bare-basename matching would over-link every nested copy. Entries containing a leading `/`, an empty segment, or a `.` / `..` segment are rejected silently.

The shipped `config.default.json` value is `[]`, so projects must opt in. `/tusk-init` (and the `tusk init-wizard` CLI) seeds reasonable defaults during install based on `project_type`: `python_service` → `[".venv", ".env"]`; `web_app` → `["node_modules", ".env", ".env.local"]`; `ios_app` → no change (no canonical gitignored runtime files for iOS). Other project types and re-runs pass through whatever the user explicitly confirms via `--worktree-symlink-files`. Symlink targets are absolute (survive worktree relocation), `.git` is excluded from the walk, and existing worktree paths are skipped silently (no overwrite). Individual symlink failures are best-effort and never abort worktree creation.

**Canonical-fallback when `worktree.symlink_files` is empty (issue #854):** install.sh-only installs never invoke the `init-write-config` auto-seed above, so `worktree.symlink_files` stays at `[]` even for web/python projects that obviously need it. When the configured list resolves empty AND `TUSK_NO_AUTO_SYMLINK` is unset, `task-worktree create` falls back to a canonical name set — `node_modules`, `.venv`, `.env`, `.env.local` — and runs the same walker against the primary. When ≥1 symlink is created via this path, stderr emits a one-line advisory naming the linked basenames and pointing at `/tusk-update` so the implicit list can be made explicit. Explicit config always wins: a non-empty `worktree.symlink_files` suppresses the fallback even when its contents are a strict subset of canonicals (no canonical names leak in). Set `TUSK_NO_AUTO_SYMLINK=1` to disable the fallback entirely (no advisory, no symlinks).

## Running the test suite

```bash
python3 -m pytest tests/ -v          # run all tests
python3 -m pytest tests/unit/ -v     # unit tests only (pure in-memory, no real DB)
python3 -m pytest tests/integration/ -v  # integration tests only (requires a working tusk installation)
```

Integration tests initialize their own temporary database automatically via a pytest fixture — no manual `tusk init` is needed.

**CI gate (issue #1098):** local commits and `tusk merge` only run the unit suite (the configured `test_command` is `python3 -m pytest tests/unit/ -q`) — the integration suite is too slow (~18–25 min) to gate every commit. Instead, `.github/workflows/integration.yml` runs `python3 -m pytest tests/integration/` on pull requests to `main` and pushes to `main`, so the integration suite cannot silently rot. The suite is hermetic (it builds its own git repos and stubs `gh` via PATH), so no network or secrets are needed. The whole integration suite runs in the gate. TASK-668 fixed the one previously-deselected flaky test (`test_criteria_done_silent_exit_guard.py::test_parallel_criteria_done_locked_db_reports_actionable_errors`): its parallel `criteria done` processes were intermittently dying inside `snapshot_db`'s backup-retention sweep — a sibling process `rm`'d a backup file between this process's glob expansion and its `ls -1t`, and under `set -euo pipefail` that `ls` failure aborted the whole `tusk` invocation before it dispatched to the Python helper, so the #1022 OperationalError diagnostic never ran and the #785 silent-exit guard fired instead. The retention pipeline now swallows that race with a trailing `|| true`, and the test is back in the gate. TASK-681 (issue #1085) later found that fix necessary but not sufficient: a rarer flake remained because the test's tight `communicate(timeout=10)` tripped under load — `criteria done` runs several git subprocesses (`_git_head_metadata`/`_has_new_commits_over_default`/`_head_task_id`) against the host repo *before* it reaches the lock-failing UPDATE that emits the diagnostic, and in parallel those occasionally exceeded 10s even though every diagnostic was correct. The per-process timeout was raised to 120s (the wait is bounded, so a large ceiling cannot mask a real deadlock); the strict diagnostic-content assertions are unchanged.

Dev dependencies (pytest) are listed in `requirements-dev.txt`. Install with:

```bash
pip install -r requirements-dev.txt
```

Tests live under `tests/unit/` (pure in-memory, no real DB) and `tests/integration/` (spin up a real DB via `tusk init`). Add new tests in the appropriate subdirectory following the existing patterns.

### Test gate, precheck, and the precheck-verdict reuse loop

`tusk commit`'s test gate runs the resolved `test_command` before staging. Its timeout is resolved by `load_test_command_timeout` (env `TUSK_TEST_COMMAND_TIMEOUT` → config `test_command_timeout_sec` → auto-scale → 240s default). The auto-scale ceiling is `max(static_floor, ceil(p95(last-20-successes) * 2), ceil(max_recent_success * 1.5))` — the max-recent floor (issue #1062) keeps a warm-dominated history from producing a ceiling below an already-observed cold run. When an **auto-scaled** ceiling is hit on a run that was still producing output (progressing, not a silent hang), the gate retries once with a 2× ceiling before aborting with exit 5; explicit env/config/default ceilings and silent hangs are never auto-retried.

`tusk test-precheck` runs `test_command` against HEAD (stashing local changes) and emits a JSON verdict: `{pre_existing, exit_code, test_command, stashed, diverged_from_default, diverged_paths}`. It also records the verdict in the `precheck_verdicts` table keyed by `(head_sha, test_command)`. **Verdict reuse (issue #1083):** when `tusk commit`'s gate then fails, it looks up a same-HEAD, same-command `pre_existing=true` verdict (within 24h) and lands the commit through the normal staging/lint/criterion path with a `[test-precheck-bypass]` note in the message instead of refusing — no more hand-rolled `git commit` fallback for proven-pre-existing failures. Extra precheck signals: `diverged_from_default` (issue #1082 — `origin/<default>` has commits HEAD lacks touching the failing files; rebase before concluding pre-existing) and, with `--flake-retries N`, `flaky_suspect`/`flake_runs_total`/`flake_failures` (issue #1076 — the runs disagree on identical HEAD, so retry the commit rather than diagnose). `task-start` additionally prints a convergence-recency hint (issue #1048) when commits touching description-named files landed near the task's filing and reference another `[TASK-N]`.

### macOS case-insensitive filesystem: realpath does NOT canonicalize case

On macOS, `os.path.realpath` resolves symlinks but **does not** canonicalize letter case. A path like `/Repo/src` and `/repo/src` may refer to the same directory, but `realpath` will return whichever case you passed in — unchanged. Do **not** mock `os.path.realpath` to simulate case canonicalization in macOS filesystem tests (e.g., mapping a wrong-case path to its canonical form). That behavior does not exist on macOS and produces false-positive test results. To test case-insensitive FS handling, use `@pytest.mark.skipif(sys.platform != "darwin", ...)` and exercise the actual path-comparison logic (e.g., `_escapes_root()`) directly.

## Architecture

### Single Source of Truth: `bin/tusk`

The bash CLI resolves all paths dynamically. The database lives at `<repo_root>/tusk/tasks.db`. Everything references `bin/tusk` — skills call it for SQL, Python scripts call `subprocess.check_output(["tusk", "path"])` to resolve the DB path. Never hardcode the database path.

**Cross-repo CWD pinning.** `bin/tusk` resolves `REPO_ROOT` by walking up from `$PWD` to the nearest `.git`. Changing CWD to a different git repo (e.g., a consumer project during a cross-repo task) would otherwise silently reroute every tusk command to that repo's database. Two env-var overrides guard against this:

- **`TUSK_PROJECT=<path>`** — pins `REPO_ROOT` (and therefore `DB_PATH`, `config.json`, etc.) to the given path regardless of CWD. Use this when working in a consumer repo while operating on the originating project's tusk DB.
- **`TUSK_DB=<path>`** — pins only the DB path (unchanged escape hatch used by migrations and tests).

When neither override is set and an active session exists for a different project (tracked in `$TUSK_STATE_DIR/active-projects`, default `~/.tusk/active-projects`), tusk emits a stderr warning listing the pinned projects and the mismatched CWD — but only when stderr is a TTY. Agent callers (Claude Code), piped stderr, and CI runs are silent by default, since their captured stderr lands back in LLM context and clutters it without a human to read it. `task-start` registers the current `REPO_ROOT`; `session-close` (and the bulk `--task-id` path) deregister it when no open sessions remain. `TUSK_QUIET=1` forces silence in any context; `TUSK_FORCE_WARN=1` restores the warning when stderr isn't a TTY (used by the drift regression tests).

**Debug env vars.** Two knobs are useful when isolating a silent-exit or unexpected-error path in `bin/tusk`:

- **`TUSK_SILENT_EXIT_GUARD=0`** — disable the recursion-guarded inner-stderr capture (issue #785). Off-by-default-when-attached; turn it off when the guard's "exited N with no diagnostic output" message itself is in the way.
- **`TUSK_TRACE=1`** — enable `set -x` shell tracing in `bin/tusk`. Pair it with stderr redirection to capture a full transcript: `TUSK_TRACE=1 tusk skill-run finish 1927 2> trace.log`. Activates after the silent-exit guard so trace output isn't swallowed. Also exports `TUSK_TRACE_ACTIVE=1` so nested `tusk` invocations and Python helpers can opt into matching verbose modes (issue #800).

**Concurrency under parallel worktree sessions (issue #1143).** Many `/tusk` sessions in separate worktrees share one project `tasks.db`. Two layers in `bin/tusk-db-lib.py` keep a transient writer collision from hard-failing a command with `OperationalError: database is locked`:

- **`get_connection` opens with `PRAGMA busy_timeout` AND `isolation_level = "IMMEDIATE"`.** busy_timeout (issue #946, env `TUSK_BUSY_TIMEOUT_MS`, default 5000) makes a writer wait for a held lock. But busy_timeout is NOT honored when a connection holding a SHARED read lock (e.g. an open SELECT cursor) promotes to a writer — SQLite returns SQLITE_BUSY immediately to avoid deadlock. `isolation_level="IMMEDIATE"` makes implicit write transactions acquire the lock up front via `BEGIN IMMEDIATE`, where busy_timeout IS honored, eliminating the common upgrade case.
- **`retry_on_locked` / `run_write` retry the whole unit of work with bounded exponential backoff** for the residual upgrade/open-cursor cases that even IMMEDIATE can't avoid — each attempt opens a fresh connection so the SHARED lock is dropped between tries. Tunable via env `TUSK_WRITE_RETRIES` (default 6 retries after the first try) and `TUSK_WRITE_RETRY_BASE_MS` (default 25ms, doubling per attempt, capped at 2s). On budget exhaustion a one-line actionable stderr diagnostic is emitted (not a raw traceback) and the original `OperationalError` is re-raised. High-frequency write paths — `skill-run` finish/start/cancel, `criteria`, `review`, `progress`, `context`, `jot` — are routed through this layer; raise the knobs under unusually heavy parallel load. (This is why convention 50's "serialize tusk write commands" guidance is now a soft preference rather than a hard requirement.)

### Config-Driven Validation

`config.default.json` defines domains, task_types, statuses, priorities, closed_reasons, complexity, criterion_types, and agents. On `tusk init`, SQLite validation triggers are **auto-generated** from the config via an embedded Python snippet in `bin/tusk`. Empty arrays (e.g., `"domains": []`) disable validation for that column. After editing config post-install, run `tusk regen-triggers` to update triggers without destroying the database (unlike `tusk init --force` which recreates the DB).

The config also includes a `review` block: `mode` (`"disabled"` or `"ai_only"`), `max_passes`, and an optional `reviewer` object (`{name, description}`). Top-level `review_categories` and `review_severities` define valid comment values — empty arrays disable validation.

**Adding a new top-level key to `config.default.json`:** You must also add the key to `KNOWN_KEYS` in `bin/tusk-config-tools.py` (line ~34). Rule 7 of the config linter validates that every key in `config.default.json` is present in `KNOWN_KEYS` — if it's missing, `tusk init` and `tusk validate` will both fail with a Rule 7 violation.

**Worktree config-edit verification (issue #767):** `tusk config` and `tusk validate` always read the **primary checkout's** `tusk/config.json` even when invoked from a task worktree — this is the deliberate shared-config invariant that mirrors the shared DB. Each invocation now prints `Config: <resolved-path>` to stderr so operators can confirm which file was read. To verify branch-local edits to `tusk/config.json` in a worktree, `cat tusk/config.json` from the worktree directly; the tusk subcommands are not aware of unmerged worktree config changes.

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
- **`/tusk-insights`** — Read-only DB health audit + on-demand HTML task dashboard
- **`/investigate`** — Scope a problem via Plan Mode and propose remediation tasks for `/create-task`
- **`/investigate-directory`** — Audit a directory's purpose and alignment with the tusk client project
- **`/resume-task`** — Recover session from branch name + progress log
- **`/chain`** — Parallel dependency sub-DAG execution (one or more head IDs)
- **`/objective`** — Run an objective end to end: create it, decompose into tasks via `/create-task`, drive the linked sub-DAG to Done in parallel waves via `/chain`, roll up via `tusk objective brief`, and close via `tusk objective done`
- **`/loop`** — Autonomous backlog loop; dispatches `/chain` or `/tusk` until the backlog drains, then surfaces `tusk propose-work` candidates (proposals only — never auto-created)
- **`/review-commits`** — Parallel AI code review; fixes must_fix findings, and fixes, preserves as context, dismisses, or spins suggest findings into follow-up tasks
- **`/address-issue`** — Fetch a GitHub issue, score it, create a tusk task, and work through it
- **`/ios-libs-issue`** — File an issue against the configured iOS lib repo (`project_type=ios_app` only); auto-attaches originating tusk task
- **`/ios-libs-contribute`** — Open a PR against the configured iOS lib repo (`project_type=ios_app` only); links the upstream PR back to the originating tusk task
- **`/report-tusk-issue`** — File an issue against the tusk repo itself (bugs, CLI limitations, missing features); approval-gated, with configurable attribution footer

### Hooks (installed to `.claude/hooks/` and `.git/hooks/` in target projects)

There are two parallel guard surfaces. Both are populated by `install.sh`; see `docs/HOOKS.md` for the dispatcher contract and instructions for adding a new guard.

- **Agent-time hooks** (`.claude/hooks/<name>.sh`) — invoked by Claude Code's `SessionStart` and `PreToolUse` (typically `Bash`) machinery. Used to inject in-progress task context, surface unconfirmed `skill-patch:<file>` retro_findings (`surface-skill-patches.sh` calls `tusk retro-patches --window-days 30 --unconfirmed` so the agent can file a `skill-patch-confirmed:<file>` finding via `/retro` once the patched behavior is observed to hold), block sqlite shell-outs, run advisory lint, etc. Claude-Code-only — Codex installs skip this directory.
- **Git-event hooks** (`hooks/git/<name>.sh` + `.git/hooks/<event>` dispatchers) — invoked by git itself on commit/push, so they fire in both agent and human workflows. The dispatcher at `.git/hooks/<event>` is generated by `install.sh`, carries the `TUSK_HOOK_DISPATCHER_V1` marker for idempotent re-runs, and chains any pre-existing user hook to `.git/hooks/<event>.pre-tusk` so external hooks are preserved rather than overwritten.

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

**Source-repo auto-refresh of `.claude/bin/` after `tusk merge` (issue #863):** in the tusk source repo, `bin/` is the source of truth and `.claude/bin/` is a refresh-on-demand cache populated by `tusk dev-sync`. When `tusk merge` finishes in a primary checkout that has a `.claude/bin/` directory, it compares each `bin/tusk-*.py` (and `bin/tusk`) against its deployed counterpart and, if any pair drifts, invokes `tusk dev-sync` automatically before returning. A single stderr line names the drifted files. Consumer installs (no `.claude/bin/` in the primary) are a silent no-op. Set `TUSK_NO_DEPLOYED_BIN_REFRESH=1` to disable. This closes the gap where source-repo fixes to `bin/tusk-*.py` shipped via merge but did not take effect for subsequent same-session tasks because `PreToolUse` hooks kept loading the stale `.claude/bin/` copies.

**No-checkout merge cleanup exit code (TASK-504):** when the no-checkout fast-forward path finishes (push to `origin/<default>` + session-close + task-done all succeeded) but `_cleanup_no_checkout_workspace` cannot remove the recorded task worktree or delete the local feature branch — typically because the worktree contains untracked files outside the auto-symlink set, or `git branch -D` failed — `tusk merge` exits **3** instead of **0**. This is the distinct non-fatal "succeeded but cleanup needs manual attention" signal: the task is Done, the work shipped to origin, but a leftover worktree directory and/or local feature branch remain. Automation can check `$?` directly rather than grepping stderr for `git worktree remove ... failed`. The earlier exit codes are preserved: a non-zero `_close_completed_task` (task-done failure) still surfaces as exit **2** even if cleanup also failed — the more severe signal wins.

**No-checkout strand guard is patch-id aware + `--allow-diverged-default` escape hatch (issue #1102):** the strand guard (`_local_default_unpushed_commits` in `bin/tusk-merge.py`) protects against pushing a feature branch that would silently strand commits living only on local `<default>`. It used pure SHA reachability (`git log origin/<default>..<default>`), which false-positived when a local commit's *content* was already published to origin under a different SHA (rebase-publish): the no-checkout path aborted with exit 2 even though nothing would actually be lost. The result is now filtered through `_drop_patch_id_published_commits`, which runs `git cherry origin/<default> <default>` and drops every commit git marks `-` (patch already upstream), keeping only the genuinely-new `+` commits. Both strand guards consume `_local_default_unpushed_commits` — the no-checkout `_warn_no_checkout_unpushed_default` path and the standard-checkout `_confirm_proceed_with_unpushed` path — so both inherit the patch-id awareness. A `git cherry` failure degrades gracefully to the prior SHA-based list (conservative). For the remaining genuinely-diverged case where the primary's local `<default>` has real unpushed commits AND its tree is dirty (so neither `git pull --rebase` nor `git reset --hard` is safe to reconcile it), pass `--allow-diverged-default` to `tusk merge`: the no-checkout path then warns and names the stranded commits but proceeds with the push instead of aborting, leaving those commits on local `<default>` for the operator to reconcile afterward.

**No-checkout merge auto-sync of primary (issue #880):** the no-checkout fast-forward path pushes to `origin/<default>` without updating primary's working tree, so after `tusk merge` returns from a sibling-worktree closeout the primary checkout is still behind origin. The end-of-merge advisory (`_maybe_advise_stale_deployed_bin` in `bin/tusk-merge.py`) now invokes `tusk sync-main` automatically against primary_root when the source-repo-layout + clean-`git status` gates pass — sync-main internally fetches, ff-pulls, stashes by ref if needed, and runs `tusk migrate`. On success, stderr emits `tusk: auto-synced primary to origin/<default> via tusk sync-main ...` (replacing the four-variant advisory established by issue #877) and the deployed-bin refresh chains automatically so any new `bin/tusk-*.py` content pulled by sync-main propagates to `.claude/bin/`. On non-zero exit, TASK-493 (issue #908) routes the advisory by sync-main failure mode: unmerged paths take priority and emit `primary has unmerged paths` with the offending sample, otherwise stderr is classified into one of `fetch`/`stash`/`ff`/`pop`/`migrate` to recommend a step-specific recovery. Indeterminate failures (no unmerged paths, no recognized stderr signature) fall through to `tusk: auto-sync failed (tusk sync-main exit N) — fall back to manual recovery below.` followed by the issue #877 four-variant wording so the operator still has the manual recovery command. **Issue #915 — indeterminate fallback now surfaces sync-main's captured stderr verbatim** under a `sync-main stderr:` header between the exit-code prefix and the four-variant advisory body, so the operator sees the underlying failure reason instead of having to re-run sync-main by hand. Empty or whitespace-only stderr leaves the original wording unchanged; the routed cases (unmerged paths / fetch / stash / ff / pop / migrate) already include focused diagnostic context and are unaffected. Set `TUSK_NO_AUTO_SYNC_MAIN=1` to keep the four-variant advisory and skip the auto-action (the same `TUSK_NO_DEPLOYED_BIN_REFRESH=1` master switch still suppresses both helpers entirely). This closes the operator-action gap left after TASK-461 (issue #877), which changed *what the advisory recommends* but not *whether the operator has to act on it*.

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

**Public skill** (distributed to target projects):
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

Bump for **any change delivered to a target project**: new/modified skill, CLI command, Python script, schema migration, `config.default.json`, or `install.sh`. **Do NOT bump** for repo-only changes (README, CLAUDE.md, task database).

```bash
echo 14 > VERSION   # increment by 1
```

Commit the bump in the same branch as the feature. Also update `CHANGELOG.md` in the same commit under a new `## [<version>] - <YYYY-MM-DD>` heading. **One VERSION bump per PR.**

**Double-bump guard (issue #1109):** after writing the new value, `tusk version-bump` resolves the VERSION committed on `origin/<default>` (`git show origin/<default>:VERSION`, falling back to the local default branch when origin is unreachable) and emits a one-line stderr warning when the new value is more than 1 ahead of that baseline. This catches the common accidental double-bump (running `version-bump` twice silently lands VERSION 2+ ahead even though the rule is one bump per PR, as observed in TASK-672). The warning is **advisory, not a refusal** — the bump still writes, stages, and exits 0 — so the rare legitimate over-bump stays unblocked while the mistake becomes loud. When the baseline cannot be resolved (no origin ref and no local default), no warning is emitted. **`tusk changelog-add` surfaces the same warning (issue #1111):** because version-bump and changelog-add are normally run together, changelog-add reuses the same `resolve_default_baseline_version` resolver and emits an equivalent one-line advisory stderr warning when the resolved CHANGELOG version lands more than 1 ahead of the `origin/<default>` baseline. It is likewise non-blocking — the CHANGELOG entry is still prepended, staged, and the command exits 0 — and silent when the baseline cannot be resolved.

> **Heads up — `tusk version-bump` and `tusk changelog-add` stage their files automatically.** After running either command, `VERSION` and `CHANGELOG.md` are already in the git index. The next `tusk commit` you run will bundle them into whatever commit you name, even if you only pass the feature files explicitly. To split the bump into its own commit, run `tusk commit <task_id> "Bump VERSION to N and update CHANGELOG" "VERSION" "CHANGELOG.md"` immediately after the bump and before any other `tusk commit` call.
>
> **Worktree routing — the resolution key is the invoking checkout's `REPO_ROOT`, NOT the active task's worktree branch (issue #903).** Both commands walk up from `$PWD` to the nearest `.git` and write VERSION/CHANGELOG.md against that checkout. Run them from inside the task worktree and the bump lands there cleanly (issues #798/#801). Run them from the primary checkout while it is on the default branch and the bump silently lands in the primary instead — primary is on `main`, no worktree branch matches, so there is no auto-routing to fall back to. To bump a task worktree's files from any CWD (typically primary), pass `--task-id`:
>
> ```bash
> tusk version-bump --task-id <N>
> tusk changelog-add --task-id <N> [<task_id>...]
> ```
>
> The CLI resolves the matching workspace via the `task_workspaces` registry and writes/stages against `workspace_path`. Both commands refuse with a clear error if `--task-id` is passed but the task has no recorded workspace or the workspace path no longer exists on disk.

## Prompting Efficiency Metric

The `tusk dashboard` Cost tab includes a "Cost Per User Prompt (Weekly)" trend that reads from `skill_runs.user_prompt_tokens` and `skill_runs.user_prompt_count`. **The metric to optimize is `cost_per_user_prompt`, not raw token count.** Terse is not better: a clear-but-verbose prompt that prevents three rounds of clarification beats a cryptic one-liner that triggers iteration. Watch the dollar trend, not the token count — falling cost-per-prompt over time means your prompts are doing more work per turn.

`tusk skill-run list` surfaces the per-run companion as `T/Msg` (tokens per user message). It's an estimate (chars/4), so the absolute number is rough — the trend is what matters.

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

<!-- tusk-task-tools -->
## Tusk Task Lookup

**Do NOT use your agent's built-in `TaskList`, `TaskGet`, or `TaskUpdate` tools to look up or manage tasks.** Those tools manage background agent subprocesses, not tusk tasks.

Use the tusk CLI instead:
- `tusk task-list` — list tasks
- `tusk task-get <id>` — get a task by ID (accepts `506` or `TASK-506`)
- `tusk task-update <id>` — update a task
