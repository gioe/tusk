# SCRIPTS.md

Reference for every `bin/tusk-*.py` file: what CLI command(s) it implements,
what it reads/writes, and how it relates to other scripts.

---

## Library Modules

These files have **no `__main__` entry point** and are never invoked directly.
They are imported by command scripts via `importlib` (hyphenated filenames
require it).

> **Rule 8 convention for new library scripts.** The `tusk lint` Rule 8
> (orphaned `tusk-*.py` scripts) looks for the literal filename string
> (e.g., `tusk-json-lib.py`) in either `bin/tusk` or another script — a bare
> `tusk_loader.load("tusk-json-lib")` call is **not** enough to satisfy it,
> because the loader argument uses the stem without the `.py` suffix. When
> adding a new library script, add a comment of the form
> `import tusk_loader  # loads tusk-<name>.py` in at least one consumer.
> That single literal reference clears Rule 8 and documents the dependency.

| File | Role |
|------|------|
| **tusk-db-lib.py** | Shared database and config utilities. Provides `get_connection()` (opens `tasks.db` with FK enforcement via `PRAGMA foreign_keys = ON`) and `load_config()`. Imported by almost every command script. |
| **tusk-json-lib.py** | Shared JSON stdout helper. Provides `dumps(obj)` which returns compact single-line JSON by default (`separators=(",", ":")`, `ensure_ascii=False`) for agent consumption, or indented JSON when `TUSK_PRETTY=1` is set in the environment. The `bin/tusk` bash dispatcher translates a global `--pretty` flag into `TUSK_PRETTY=1` before invoking any Python script. Imported by every command script that prints JSON to stdout. |
| **tusk-pricing-lib.py** | Shared transcript-parsing and cost-computation utilities. Provides pricing data loading, model resolution, JSONL transcript iteration, and per-session token/cost aggregation. Imported by: `tusk-session-stats.py`, `tusk-criteria.py`, `tusk-session-recalc.py`, `tusk-call-breakdown.py`, `tusk-skill-run.py`, `tusk-dashboard-data.py`. |
| **tusk-dashboard-data.py** | Data-access layer for the HTML dashboard. Provides `get_connection()` and all `fetch_*` functions that query the DB. Imported by `tusk-dashboard.py` and `tusk-dashboard-html.py`. |
| **tusk-dashboard-html.py** | HTML-generation layer for the dashboard. Contains all templating functions: formatters, component generators, and section builders. Imported by `tusk-dashboard.py`. Depends on `tusk-dashboard-css.py` and `tusk-dashboard-js.py`. |
| **tusk-dashboard-css.py** | CSS stylesheet bundle for the dashboard, extracted to reduce file size. Exposes a single `CSS` string constant. Imported by `tusk-dashboard-html.py`. |
| **tusk-dashboard-js.py** | JavaScript bundle for the dashboard, extracted to reduce file size. Exposes a single `JS` string constant. Imported by `tusk-dashboard-html.py`. |

---

## Command Scripts

### Task Lifecycle

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-task-brief.py** | `tusk task-brief <id> [--format json\|markdown]` | `tasks`, `acceptance_criteria`, `task_scope`, `task_dependencies`, `task_progress`, `objectives`, `objective_tasks`, `task_context_items` | nothing |
| **tusk-task-get.py** | `tusk task-get <id>` | `tasks`, `acceptance_criteria`, `task_progress`, `objective_tasks`, `objectives` | nothing |
| **tusk-task-insert.py** | `tusk task-insert "<summary>" "<description>" [flags]` | config, `tasks` (dupe check) | `tasks`, `acceptance_criteria` |
| **tusk-task-list.py** | `tusk task-list [--status] [--domain] [--assignee] [--workflow] [--objective] [--format] [--all]` | `tasks`, `objective_tasks` | nothing |
| **tusk-task-select.py** | `tusk task-select [--max-complexity] [--exclude-ids]` | `v_ready_tasks`, config | nothing |
| **tusk-task-start.py** | `tusk task-start <id> [--force] [--force-deps] [--force-contingent] [--force-not-before] [--force-session] [--agent NAME] [--skill NAME]` | `tasks`, `task_progress`, `task_sessions`, `acceptance_criteria` | `tasks` (status), `task_sessions` (new session), `skill_runs` (when `--skill` is supplied) |
| **tusk-task-update.py** | `tusk task-update <id> [flags]` | `tasks`, `acceptance_criteria`, `task_scope`, config | `tasks`, `task_scope` (`auto_derived` rows refreshed when summary/description changes) |
| **tusk-task-done.py** | `tusk task-done <id> --reason <reason> [--force]` | `tasks`, `acceptance_criteria`, `task_sessions`, `task_dependencies` | `tasks`, `task_sessions`, `acceptance_criteria` |
| **tusk-task-reopen.py** | `tusk task-reopen <id> --force` | `tasks` | `tasks` (status reset to To Do) |

### Dev Workflow

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-branch.py** | `tusk branch <id> <slug>` | git remote/HEAD | git (creates feature branch) |
| **tusk-commit.py** | `tusk commit <id> "<msg>" <files…> [--criteria <id>…] [--skip-verify]` or `tusk commit <id> <files…> -m "<msg>" [--criteria <id>…] [--skip-verify]` | config (`test_command`), staged files | git (stages + commits), `acceptance_criteria` (via `tusk criteria done`) |
| **tusk-merge.py** | `tusk merge <id> [--session <id>] [--pr] [--pr-number N] [--rebase] [--skip-lint] [--skip-verify]` | `tasks`, `task_sessions`, config (`merge.mode`, `lint_timeout_sec`) | `task_sessions` (close), `tasks` (Done), git (merge + push + branch delete) after clean `tusk lint` unless `--skip-lint` skips only the pre-merge lint gate or `--skip-verify` skips lint plus future pre-merge verification gates |
| **tusk-progress.py** | `tusk progress <id> [--note "…"] [--next-steps "…"]` | git HEAD | `task_progress` |
| **tusk-criteria.py** | `tusk criteria add\|list\|done\|skip\|reset\|delete <id> [flags]` | `acceptance_criteria`, git HEAD, Claude Code transcripts | `acceptance_criteria`; cost attribution via `tusk-pricing-lib.py` |
| **tusk-context.py** | `tusk context add\|list\|resolve\|supersede ...` | `tasks`, `objectives`, `task_context_items` | `task_context_items` (except `list`) |
| **tusk-objective.py** | `tusk objective insert\|list\|get\|brief\|update\|link\|unlink\|done ...` | `objectives`, `objective_tasks`, `tasks`, `task_metrics`, `v_criteria_coverage`, `task_context_items` (brief rollup) | `objectives`, `objective_tasks` |

### Dependencies

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-deps.py** | `tusk deps add\|remove\|list\|dependents\|blocked\|ready\|all [flags]` | `task_dependencies`, `tasks` | `task_dependencies` (add/remove); DFS cycle detection on add |

### Init Helpers

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-init-intent.py** | `tusk init-intent normalize --answers '<json>'` | raw JSON answers | nothing; emits normalized `init_intent` JSON |
| **tusk-init-write-config.py** | `tusk init-write-config [flags]` | `tusk/config.json`, `config.default.json` | `tusk/config.json`; validation triggers via `tusk regen-triggers` |
| **tusk-init-wizard.py** | `tusk init-wizard [flags]` | config, codebase scan, test detection, bootstrap manifests | config, optional scaffold files, optional seeded tasks |
| **tusk-init-scaffold.py** | `tusk init-scaffold --spec '<json>'` | install mode and scaffold spec | directories, `.gitkeep`, routing stubs |
| **tusk-init-fetch-bootstrap.py** | `tusk init-fetch-bootstrap` | `project_libs` config, remote `tusk-bootstrap.json` manifests | nothing |
| **tusk-init-write-manifest-files.py** | `tusk init-write-manifest-files --spec '<json>'` | manifest file spec | create-only files and append-if-missing snippets |
| **tusk-init-scan-codebase.py** | `tusk init-scan-codebase` | manifests and repo directories | nothing |
| **tusk-init-scan-todos.py** | `tusk init-scan-todos` | source comments | nothing |

### Backlog Management

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-autoclose.py** | `tusk autoclose` | `tasks`, `task_sessions`, `task_dependencies` | `tasks` (expired/moot → Done), `task_sessions` (close open) |
| **tusk-backlog-scan.py** | `tusk backlog-scan [--duplicates\|--unassigned\|--unsized\|--expired]` | `tasks` | nothing; returns JSON |
| **tusk-blockers.py** | `tusk blockers add\|list\|resolve\|remove\|blocked\|all [flags]` | `tasks`, `external_blockers` | `external_blockers` |
| **tusk-propose-work.py** | `tusk propose-work [--window-days N] [--limit N] [--no-todo-scan] [--no-cost-outliers]` | `retro_findings`, `task_progress`, `tasks`, `jots`, `tool_call_stats`, repo TODO/FIXME scan | nothing; returns a ranked JSON array of origination proposals (each with a `source` label + numeric `score`). Read-only — never inserts tasks; aggregates unconfirmed skill-patch findings, unconsumed next_steps, recurring jot categories, a repo TODO/FIXME scan, and (stretch) cost outliers. Empty-signal env returns `[]`. |
| **tusk-dupes.py** | `tusk dupes check\|scan\|similar [flags]` | `tasks` | nothing; heuristic fuzzy-match, returns JSON |
| **tusk-wsjf.py** | `tusk wsjf` | `tasks`, `task_dependencies` | `tasks` (`priority_score`) |
| **tusk-chain.py** | `tusk chain scope\|frontier\|status <head_id…>` | `tasks`, `task_dependencies`, `v_chain_heads` | nothing; DFS sub-DAG traversal |
| **tusk-loop.py** | `tusk loop [--max-tasks N] [--dry-run] [--on-failure skip\|abort]` | `v_ready_tasks`, `v_chain_heads` | nothing directly; spawns `claude -p /chain` or `claude -p /tusk` subprocesses |

### Reviews & Cost Analysis

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-review.py** | `tusk review start\|begin\|add-comment\|list\|resolve\|validate-comments\|approve\|request-changes\|status\|summary\|verdict\|pass-status\|backfill-cost [flags]` | `code_reviews`, `review_comments`, `tasks` | `code_reviews`, `review_comments` |
| **tusk-call-breakdown.py** | `tusk call-breakdown --task\|--session\|--skill-run\|--criterion <id>` | Claude Code JSONL transcripts, `task_sessions`, `tool_call_events` | `tool_call_stats` (optional write); depends on `tusk-pricing-lib.py` |
| **tusk-cost.py** | `tusk cost [--format json\|text]` | `task_sessions`, `skill_runs` | nothing; cumulative project-cost rollup with tusk skill-run shadow de-duplication |

### Config, Lint & Setup

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-config-tools.py** | `tusk validate` / `tusk regen-triggers` | `config.json` | nothing (`validate`); DB triggers (`regen-triggers`) |
| **tusk-skill-drift.py** | `tusk skill-drift [--format text\|json] [--is-referenced <subcmd>]` (also run advisory by `tusk validate`) | installed `SKILL.md` files + the sibling `bin/tusk` dispatcher | nothing; reports `tusk <subcommand>` references absent from the installed CLI and recommends `tusk upgrade` (issue #1035); exit 1 on drift |
| **tusk-migrate.py** | `tusk migrate` | `tasks.db` (`PRAGMA user_version`) | DB schema (applies pending migrations in order) |
| **tusk-setup.py** | `tusk setup` | `tasks.db`, `config.json` | nothing; returns config + backlog JSON in one call |
| **tusk-lint.py** | `tusk lint` | repo files, `tasks.db`, config, `MANIFEST` | nothing; advisory output only; depends on `tusk-db-lib.py` |
| **tusk-lint-rules.py** | `tusk lint-rule add\|propose\|list\|update\|promote\|remove [flags]` | `lint_rules`, `retro_findings` | `lint_rules` |
| **tusk-test-detect.py** | `tusk test-detect` | `package.json`, lockfiles, `pyproject.toml`, `pytest.ini`, `Makefile`, etc. | nothing; returns `{"command": "…", "confidence": "…"}` |

### Sessions & Cost Tracking

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-session-stats.py** | `tusk session-stats <session_id> [transcript_path]` | Claude Code JSONL transcripts, `task_sessions` | `task_sessions` (`tokens_in`, `tokens_out`, `cost_dollars`, `model`); depends on `tusk-pricing-lib.py` |
| **tusk-session-recalc.py** | `tusk session-recalc` | `task_sessions`, Claude Code JSONL transcripts | `task_sessions` (recomputes cost for all sessions); depends on `tusk-pricing-lib.py` |
| **tusk-skill-run.py** | `tusk skill-run start\|finish\|cancel\|list [flags]` | `skill_runs`, Claude Code JSONL transcripts | `skill_runs`; depends on `tusk-pricing-lib.py` |
| **tusk-token-audit.py** | `tusk token-audit [--summary\|--json]` | skill SKILL.md files in `skills/`, `skills-internal/`, `.claude/skills/` | nothing; advisory analysis only |

Transcript discovery for session, review, criterion, call-breakdown, and skill-run cost attribution is centralized in `tusk-pricing-lib.py`. When called from a task-owned worktree, it tries the current directory, the Git toplevel, the primary checkout reached through `git rev-parse --git-common-dir`, then parent directories, deriving the matching `~/.claude/projects/<hash>/*.jsonl` directory for each candidate. This lets task worktrees under `~/.tusk/worktrees/...` still find transcripts recorded against the primary checkout. `tusk skill-run finish` records `model = '(transcript missing)'` when that search finds no JSONL at all; this is distinct from `'(unknown)'`, which means a transcript was found but no model-bearing request was attributable to the run window.

### Dashboard

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-dashboard.py** | `tusk dashboard` | `tasks.db` via `tusk-dashboard-data.py` | writes a static HTML file and opens it in the browser; directly imports `tusk-dashboard-data.py` and `tusk-dashboard-html.py` (which in turn loads `tusk-dashboard-css.py` and `tusk-dashboard-js.py`) |

### Versioning & Distribution

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-changelog-add.py** | `tusk changelog-add [--from-version-file] [<version>] [<task_id>…]` | `tasks` (summaries), `VERSION`, `CHANGELOG.md` | `CHANGELOG.md` (prepends new entry) |
| **tusk-generate-manifest.py** | `tusk generate-manifest` (also used by `tusk-lint.py` rule18) | `skills/`, `bin/`, `config.default.json`, `VERSION`, `install.sh` | `MANIFEST` (sorted JSON array) |
| **tusk-pricing-update.py** | `tusk pricing-update [--dry-run]` | Anthropic pricing page (HTTPS) | `pricing.json` |
| **tusk-sync-skills.py** | `tusk sync-skills` | `skills/`, `skills-internal/` | `.claude/skills/` (recreates symlinks) |
| **tusk-reconcile-skills.py** | `tusk reconcile-skills [--source-dir <p>] [--dry-run] [--quiet] [--json]` | `tusk/config.json:project_type`, source `skills/` (local or `--source-dir`) | installs / removes `applies_to_project_types`-gated entries under `.claude/skills/` to match the current `project_type` |
| **tusk-upgrade.py** | `tusk upgrade [--no-commit] [--force]` | GitHub releases tarball, `VERSION` | copies updated files to `.claude/bin/` and `.claude/skills/`; runs `tusk migrate` |
| **tusk-sync-main.py** | `tusk sync-main` | `origin` git refs, working tree | git working tree (fetch + ff-only pull of `origin/<default>` + stash-by-ref + pop); runs `tusk migrate` only after post-pop safety checks pass. Recovery helper for `/address-issue` Step 4.6 staleness check |

#### `tusk sync-main` stale snapshot guard

When `tusk sync-main` stashes local changes, fast-forwards the default branch,
and then successfully pops the stash, it runs a post-pop stale snapshot guard
before invoking `tusk migrate`. The guard compares dirty paths touched by the
incoming commits against both the pre-sync HEAD and the fast-forwarded HEAD.
If a popped path now matches the pre-sync blob while the new HEAD has newer
content, `sync-main` stops with an accidental-revert warning that names the
stale path(s). At that point the fast-forward and stash pop have already
succeeded, but migrations have not run; inspect the named files and restore any
stale content before committing or rerunning `tusk migrate`.

---

## JSON output contract

Every `bin/tusk-*.py` script (and the `bin/tusk` bash dispatcher) emits JSON
to stdout in **compact** form by default — one line, `","` / `":"` separators
with no trailing whitespace, `ensure_ascii=False` so non-ASCII survives as
UTF-8 bytes instead of `\uXXXX` escapes. Agents are the primary consumer; every
byte becomes an input token on the next model turn.

Human-readable indented output (`indent=2`) is opt-in via either:

- `--pretty` — any invocation of `tusk` accepts a global `--pretty` flag; the
  bash dispatcher strips it from the argv and exports `TUSK_PRETTY=1` before
  dispatching.
- `TUSK_PRETTY=1` — set the env var directly (for shells, test harnesses, or
  piped invocations). Accepted truthy values: `1`, `true`, `yes`, `on`.

Scripts do not call `json.dumps(obj, indent=2)` on stdout paths — they import
`tusk-json-lib.py` via the shared `tusk_loader` and call `dumps(obj)`. File
writes that produce human-edited config (`config.json`, `settings.json`,
`pricing.json`, `MANIFEST`) remain pretty-printed with `indent=2` since they
are read by humans outside the agent hot path.
