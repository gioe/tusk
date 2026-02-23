# Tusk Product Pillars

This document defines the seven product pillars that guide tusk's design and development. Each pillar represents a core value the product commits to. Use this file to categorize backlog items and evaluate proposed work against which pillar it advances.

---

## Maturity Summary

| Pillar | Maturity | Status |
|---|---|---|
| Transparent | High | Core DB schema, session/cost tracking, and audit trail are solid |
| Accessible | Medium | CLI and skills are discoverable; onboarding wizard exists but setup UX has rough edges |
| Extensible | High | Config-driven validation, open skill system, and Python script pattern are well-established |
| Opinionated | High | WSJF scoring, workflow gates, and convention enforcement are baked in |
| Autonomous | Medium | `/loop` and `/chain` exist; reliability and interrupt handling still maturing |
| Observable | Medium | Dashboard and cost tracking are strong; real-time visibility and alerting are sparse |
| Self-Improving | Low | `/retro` and `/lint-conventions` exist; learning loop and convention propagation are nascent |

---

## 1. Transparent

**Definition:** Tusk makes all task state, cost, and decision history visible and auditable at any time. Every action that changes a task — status transitions, session opens and closes, criteria completions, PR links, progress checkpoints — is recorded in the database and queryable. Nothing important happens in memory only.

**Core claim:** You can always reconstruct what happened to any task, who worked on it, how much it cost, and what was decided — even weeks later with no conversation history.

**Current maturity:** High. The schema is comprehensive: `task_sessions` captures model, cost, tokens, and diff stats; `acceptance_criteria` records completion timestamps and per-criterion cost; `task_progress` preserves commit-level checkpoints with `next_steps` for session recovery; `code_reviews` and `review_comments` log reviewer findings and resolutions. The `tusk call-breakdown` command attributes cost to individual tool calls.

**Representative features:**
- `task_sessions` table with `cost_dollars`, `tokens_in`, `tokens_out`, `model`, `lines_added`, `lines_removed`
- `acceptance_criteria.completed_at`, `cost_dollars`, `tokens_in`, `tokens_out` — per-criterion cost attribution
- `task_progress` append-only checkpoint log with `commit_hash`, `files_changed`, `next_steps`
- `tusk call-breakdown` — per-tool-call cost attribution via JSONL transcript parsing
- `tusk session-stats` / `tusk session-close` — transcript ingestion and session finalization
- `code_reviews` + `review_comments` tables with resolution tracking

---

## 2. Accessible

**Definition:** Tusk is easy to install, configure, and use across different projects and team setups. The setup process requires minimal manual steps. Skills are self-documenting. Common operations are surfaced through a consistent CLI with plain-English subcommands. The system works without a database administrator, devops knowledge, or bespoke infrastructure.

**Core claim:** Any developer with Claude Code can be productive in tusk within one session — install, configure, and start working through tasks without reading a manual.

**Current maturity:** Medium. `install.sh` handles the full install in one command. `/tusk-init` provides an interactive wizard that scans the codebase and suggests config. The CLI uses plain subcommands (`task-start`, `task-done`, `criteria list`, `deps blocked`). Gaps remain: the wizard output can be verbose, error messages from SQLite triggers are opaque, and there is no guided "what do I do next?" recovery path when the backlog is empty or all tasks are blocked.

**Representative features:**
- `install.sh` — single-command install to any project
- `/tusk-init` — interactive setup wizard with codebase scanning and config suggestion
- `tusk task-start` / `tusk task-done` — consolidated workflow entry and exit commands
- `tusk deps ready` / `tusk deps blocked` — plain-English readiness queries
- `/tusk` skill default workflow — auto-selects the next task with no arguments needed
- `tusk -header -column` — readable tabular output for any SQL query

---

## 3. Extensible

**Definition:** Tusk is designed to be customized without forking. Config drives validation rules, domains, agents, task types, and priorities. Skills are composable and individually replaceable. Python scripts follow a consistent library pattern that makes adding new capabilities straightforward. New workflow steps can be introduced as new skills without modifying the core CLI.

**Core claim:** Adapting tusk to a new project's conventions requires editing `config.json`, not editing source code.

**Current maturity:** High. `config.default.json` drives SQLite trigger generation for all enum columns — adding a domain or task type is a one-line config edit followed by `tusk regen-triggers`. The skill system is open: each skill is a Markdown file with YAML frontmatter, discoverable at session startup. Python scripts use a shared library pattern (`tusk-pricing-lib.py`) with importlib for reuse. The `/reconfigure` skill updates config post-install without data loss.

**Representative features:**
- `config.default.json` with auto-generated SQLite validation triggers via `generate_triggers()`
- `tusk regen-triggers` — live trigger rebuild from config without DB recreation
- `/tusk-init` — codebase-aware config suggestion
- `/reconfigure` — post-install config updates
- `skills/` + `skills-internal/` two-tier skill distribution model
- `tusk-pricing-lib.py` shared library imported by multiple scripts via importlib
- `tusk sync-skills` — symlink regeneration for source repo skill development

---

## 4. Opinionated

**Definition:** Tusk makes strong, default decisions about how tasks should be managed so users don't have to debate process. WSJF priority scoring, complexity t-shirt sizes, required `closed_reason` on completion, status transition guards, the one-commit-per-criterion workflow, and duplicate detection are all built-in defaults that encode what good task hygiene looks like.

**Core claim:** Following tusk's defaults produces a well-maintained backlog without requiring the team to define their own process.

**Current maturity:** High. WSJF scoring is computed automatically and surfaced in `v_ready_tasks`. SQLite triggers block invalid status transitions (no `Done → In Progress`), missing `closed_reason`, and enum violations. `tusk task-start` requires at least one acceptance criterion (with `--force` override). `tusk task-done` checks for incomplete criteria before closing. The `/tusk` skill enforces the branch → implement → commit → criteria-done → PR → review → merge workflow sequence.

**Representative features:**
- WSJF `priority_score` computation: `ROUND((base_priority + source_bonus + unblocks_bonus) / complexity_weight)`
- `validate_status_transition` SQLite trigger — enforces legal status progressions
- `validate_closed_reason` trigger — blocks Done without closed_reason
- `tusk task-start --force` — zero-criteria guard with explicit override
- `tusk task-done` — incomplete-criteria check before closure
- `/tusk` skill workflow sequencing (branch → commit → criteria-done → PR → merge)
- Complexity t-shirt sizing with L/XL warning in `/tusk`

---

## 5. Autonomous

**Definition:** Tusk supports unattended operation: an agent can pick the next task, implement it, commit, push, open a PR, run review, merge, and move to the next task without human intervention. Dependency ordering, parallel execution, and backlog traversal are handled by the system, not by manual orchestration.

**Core claim:** A single `/loop` invocation can drain a well-structured backlog overnight without a human in the loop.

**Current maturity:** Medium. `/loop` implements the full autonomous backlog loop and dispatches to `/chain` for dependency sub-DAGs or `/tusk` for standalone tasks. `/chain` executes wave-by-wave parallel execution of downstream tasks. `v_ready_tasks` and `v_chain_heads` provide the correct readiness semantics. Gaps: interrupt handling when an agent gets stuck is manual; mid-chain failure recovery requires human diagnosis; the loop has no automatic retry or skip-and-continue logic for failed tasks.

**Representative features:**
- `/loop` skill — autonomous backlog traversal with `--max-tasks` and `--dry-run`
- `/chain` skill — wave-by-wave parallel sub-DAG execution with post-chain retro
- `v_ready_tasks` view — canonical "ready to work" definition used by all dispatch logic
- `v_chain_heads` view — identifies DAG entry points for chain dispatch
- `tusk loop --dry-run` — safe preview of what would run
- `tusk-loop.py` with `/chain` vs `/tusk` dispatch decision
- `tusk autoclose` — auto-closes expired, merged-PR, and moot contingent tasks

---

## 6. Observable

**Definition:** Tusk provides high-level visibility into backlog health, team velocity, cost trends, and individual task progress without requiring external tooling. The dashboard consolidates key metrics — status counts, cost by domain, DAG visualization, skill run history, and tool-call breakdowns — into a single generated HTML report. Time-series cost data enables trend analysis.

**Core claim:** You can answer "how is the project going and what is it costing?" from the dashboard alone, without querying the database manually.

**Current maturity:** Medium. The HTML dashboard (`tusk dashboard`) is feature-rich: KPIs, per-task metrics table, cost-by-domain breakdown, DAG viewer, skill runs table, tool-call stats sub-tables, cost trend charts (daily and monthly), and complexity distribution. `tusk-insights` provides a read-only DB health audit with interactive recommendations. Gaps: the dashboard is static (no live refresh), there are no threshold alerts or anomaly detection, and there is no team-level multi-project rollup.

**Representative features:**
- `tusk dashboard` — self-contained HTML report with embedded charts (Chart.js)
- `tusk-dashboard-data.py` — 17 `fetch_*` functions covering all metric dimensions
- Cost trend charts (daily and monthly) via `fetch_cost_trend_daily` / `_monthly`
- DAG visualization via Mermaid with clickable nodes
- `/tusk-insights` — interactive DB health audit across 6 categories
- `tool_call_stats` table — pre-computed per-tool-call cost aggregates
- `fetch_complexity_metrics` — complexity distribution for scope planning

---

## 7. Self-Improving

**Definition:** Tusk learns from completed work and refines its own processes over time. Post-session retrospectives surface patterns, anti-patterns, and process improvements. Generalizable conventions are written to `tusk/conventions.md` and enforced by `/lint-conventions`. Token consumption patterns are audited to keep skills lean. The system can identify and close redundant tasks, normalize the backlog, and update its own configuration as the project evolves.

**Core claim:** Each session makes the next session faster, cheaper, and less error-prone — without requiring the team to manually update process docs.

**Current maturity:** Low. `/retro` runs post-session reviews and writes conventions. `/lint-conventions` enforces those conventions via grep rules. `/token-audit` identifies bloated or redundant skill patterns. `/groom-backlog` auto-closes stale tasks and normalizes priorities. Gaps: conventions are rarely auto-surfaced in the critical path; `/lint-conventions` rules are static (no auto-generation from `/retro` output); there is no mechanism for a convention to automatically update SKILL.md files; cost-per-session trend data exists but is not used to trigger process changes.

**Representative features:**
- `/retro` skill — post-session retrospective with convention writing to `tusk/conventions.md`
- `/lint-conventions` skill — grep-based convention enforcement
- `tusk conventions` — prints learned conventions file
- `/token-audit` — skill token consumption analysis with five diagnostic categories
- `tusk-dupes.py` — heuristic duplicate detection with `difflib.SequenceMatcher`
- `/groom-backlog` — auto-close, re-prioritize, and normalize backlog in one pass
- `skill_runs` table + `tusk skill-run` — per-execution cost tracking for operational overhead awareness
