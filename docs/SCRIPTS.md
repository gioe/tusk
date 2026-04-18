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
| **tusk-task-get.py** | `tusk task-get <id>` | `tasks`, `acceptance_criteria`, `task_progress` | nothing |
| **tusk-task-insert.py** | `tusk task-insert "<summary>" "<description>" [flags]` | config, `tasks` (dupe check) | `tasks`, `acceptance_criteria` |
| **tusk-task-list.py** | `tusk task-list [--status] [--domain] [--assignee] [--format] [--all]` | `tasks` | nothing |
| **tusk-task-select.py** | `tusk task-select [--max-complexity] [--exclude-ids]` | `v_ready_tasks`, config | nothing |
| **tusk-task-start.py** | `tusk task-start <id> [--force] [--agent]` | `tasks`, `task_progress`, `task_sessions`, `acceptance_criteria` | `tasks` (status), `task_sessions` (new session) |
| **tusk-task-update.py** | `tusk task-update <id> [flags]` | `tasks`, config | `tasks` |
| **tusk-task-done.py** | `tusk task-done <id> --reason <reason> [--force]` | `tasks`, `acceptance_criteria`, `task_sessions`, `task_dependencies` | `tasks`, `task_sessions`, `acceptance_criteria` |
| **tusk-task-reopen.py** | `tusk task-reopen <id> --force` | `tasks` | `tasks` (status reset to To Do) |

### Dev Workflow

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-branch.py** | `tusk branch <id> <slug>` | git remote/HEAD | git (creates feature branch) |
| **tusk-commit.py** | `tusk commit <id> "<msg>" <files…> [--criteria <id>…] [--skip-verify]` or `tusk commit <id> <files…> -m "<msg>" [--criteria <id>…] [--skip-verify]` | config (`test_command`, `lint`), staged files | git (stages + commits), `acceptance_criteria` (via `tusk criteria done`) |
| **tusk-merge.py** | `tusk merge <id> [--session <id>] [--pr] [--pr-number N]` | `tasks`, `task_sessions`, config (`merge.mode`) | `task_sessions` (close), `tasks` (Done), git (merge + push + branch delete) |
| **tusk-progress.py** | `tusk progress <id> [--next-steps "…"]` | git HEAD | `task_progress` |
| **tusk-criteria.py** | `tusk criteria add\|list\|done\|skip\|reset <id> [flags]` | `acceptance_criteria`, git HEAD, Claude Code transcripts | `acceptance_criteria`; cost attribution via `tusk-pricing-lib.py` |

### Dependencies

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-deps.py** | `tusk deps add\|remove\|list\|dependents\|blocked\|ready\|all [flags]` | `task_dependencies`, `tasks` | `task_dependencies` (add/remove); DFS cycle detection on add |

### Backlog Management

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-autoclose.py** | `tusk autoclose` | `tasks`, `task_sessions`, `task_dependencies` | `tasks` (expired/moot → Done), `task_sessions` (close open) |
| **tusk-backlog-scan.py** | `tusk backlog-scan [--duplicates\|--unassigned\|--unsized\|--expired]` | `tasks` | nothing; returns JSON |
| **tusk-blockers.py** | `tusk blockers add\|list\|resolve\|remove\|blocked\|all [flags]` | `tasks`, `external_blockers` | `external_blockers` |
| **tusk-dupes.py** | `tusk dupes check\|scan\|similar [flags]` | `tasks` | nothing; heuristic fuzzy-match, returns JSON |
| **tusk-wsjf.py** | `tusk wsjf` | `tasks`, `task_dependencies` | `tasks` (`priority_score`) |
| **tusk-chain.py** | `tusk chain scope\|frontier\|status <head_id…>` | `tasks`, `task_dependencies`, `v_chain_heads` | nothing; DFS sub-DAG traversal |
| **tusk-loop.py** | `tusk loop [--max-tasks N] [--dry-run] [--on-failure skip\|abort]` | `v_ready_tasks`, `v_chain_heads` | nothing directly; spawns `claude -p /chain` or `claude -p /tusk` subprocesses |

### Reviews & Cost Analysis

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-review.py** | `tusk review start\|add-comment\|list\|resolve\|approve\|request-changes\|status\|summary [flags]` | `code_reviews`, `review_comments`, `tasks` | `code_reviews`, `review_comments` |
| **tusk-call-breakdown.py** | `tusk call-breakdown --task\|--session\|--skill-run\|--criterion <id>` | Claude Code JSONL transcripts, `task_sessions`, `tool_call_events` | `tool_call_stats` (optional write); depends on `tusk-pricing-lib.py` |

### Config, Lint & Setup

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-config-tools.py** | `tusk validate` / `tusk regen-triggers` | `config.json` | nothing (`validate`); DB triggers (`regen-triggers`) |
| **tusk-migrate.py** | `tusk migrate` | `tasks.db` (`PRAGMA user_version`) | DB schema (applies pending migrations in order) |
| **tusk-setup.py** | `tusk setup` | `tasks.db`, `config.json` | nothing; returns config + backlog JSON in one call |
| **tusk-lint.py** | `tusk lint` | repo files, `tasks.db`, config, `MANIFEST` | nothing; advisory output only; depends on `tusk-db-lib.py` |
| **tusk-lint-rules.py** | `tusk lint-rule add\|list\|remove [flags]` | `lint_rules` | `lint_rules` |
| **tusk-test-detect.py** | `tusk test-detect` | `package.json`, lockfiles, `pyproject.toml`, `pytest.ini`, `Makefile`, etc. | nothing; returns `{"command": "…", "confidence": "…"}` |

### Sessions & Cost Tracking

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-session-stats.py** | `tusk session-stats <session_id> [transcript_path]` | Claude Code JSONL transcripts, `task_sessions` | `task_sessions` (`tokens_in`, `tokens_out`, `cost_dollars`, `model`); depends on `tusk-pricing-lib.py` |
| **tusk-session-recalc.py** | `tusk session-recalc` | `task_sessions`, Claude Code JSONL transcripts | `task_sessions` (recomputes cost for all sessions); depends on `tusk-pricing-lib.py` |
| **tusk-skill-run.py** | `tusk skill-run start\|finish\|cancel\|list [flags]` | `skill_runs`, Claude Code JSONL transcripts | `skill_runs`; depends on `tusk-pricing-lib.py` |
| **tusk-token-audit.py** | `tusk token-audit [--summary\|--json]` | skill SKILL.md files in `skills/`, `skills-internal/`, `.claude/skills/` | nothing; advisory analysis only |

### Dashboard

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-dashboard.py** | `tusk dashboard` | `tasks.db` via `tusk-dashboard-data.py` | writes a static HTML file and opens it in the browser; directly imports `tusk-dashboard-data.py` and `tusk-dashboard-html.py` (which in turn loads `tusk-dashboard-css.py` and `tusk-dashboard-js.py`) |

### Versioning & Distribution

| File | CLI command(s) | Reads | Writes |
|------|---------------|-------|--------|
| **tusk-changelog-add.py** | `tusk changelog-add <version> [<task_id>…]` | `tasks` (summaries), `CHANGELOG.md` | `CHANGELOG.md` (prepends new entry) |
| **tusk-generate-manifest.py** | `tusk generate-manifest` (also used by `tusk-lint.py` rule18) | `skills/`, `bin/`, `config.default.json`, `VERSION`, `install.sh` | `MANIFEST` (sorted JSON array) |
| **tusk-pricing-update.py** | `tusk pricing-update [--dry-run]` | Anthropic pricing page (HTTPS) | `pricing.json` |
| **tusk-sync-skills.py** | `tusk sync-skills` | `skills/`, `skills-internal/` | `.claude/skills/` (recreates symlinks) |
| **tusk-upgrade.py** | `tusk upgrade [--no-commit] [--force]` | GitHub releases tarball, `VERSION` | copies updated files to `.claude/bin/` and `.claude/skills/`; runs `tusk migrate` |

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
