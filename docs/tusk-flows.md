# Tusk: CLI Commands × Skills — Full Flow Reference

---

## SKILL MAP (what each skill calls)

```
/tusk ──────────────────── task-start
                           branch
                           commit ──► test_command gate (hard-block, --skip-verify bypasses)
                                  ├──► lint
                                  └──► criteria done   (atomic)
                           criteria list
                           progress
                           config
                           migrate                      (advisory, post-commit)
                           lint                         (advisory, standalone)
                           merge ──► session-close
                                 └──► task-done
                           [invokes /review-commits]    (if mode = ai_only)
                           [invokes /retro]             (always, at end)

/chain ─────────────────── chain scope
                           chain frontier
                           chain status
                           criteria done                (deferred chain criteria, orchestrator)
                           [spawns /tusk agents]        (one per task in wave)
                             └── per-agent: task-start · branch · commit · criteria ·
                                           progress · lint · migrate · merge ·
                                           criteria skip --reason chain
                           [invokes /retro]             (post-chain aggregation)

/loop ──────────────────── loop                         (delegates to tusk-loop.py)
                           [tusk-loop.py spawns]
                             claude -p /chain <id>      (if chain head)
                             claude -p /tusk  <id>      (if standalone)

/review-commits ─────────── config
                            review start
                            review list
                            review status
                            review resolve
                            review approve
                            review request-changes
                            review summary
                            task-insert                  (for deferred findings)
                            [spawns reviewer agents]     (one per reviewer in config)

/retro ─────────────────── setup  (config + backlog)
                           dupes check                   (before each insert)
                           task-insert
                           [reads FOCUS.md if present]  (custom category override)
                           lint-rule (via task-insert)   (lint rule findings → task, not direct call)

/groom-backlog ─────────── skill-run start
                           setup
                           autoclose
                           dupes scan
                           deps all
                           deps blocked
                           task-done   (--reason completed / duplicate / wont_do)
                           task-update (--priority / --assignee)
                           skill-run finish ──► call-breakdown --skill-run

/create-task ───────────── config
                           setup
                           dupes check                   (via task-insert internally)
                           task-insert ──► dupes check
                                       └──► wsjf

/resume-task ───────────── task-start
                           criteria done
                           progress
                           lint
                           [continues /tusk workflow from step 4]

/dashboard ─────────────── dashboard

/token-audit ───────────── token-audit
                           setup
                           config
                           conventions
                           [invokes /create-task]        (for findings)

/tusk-update ───────────── config
                           init --force   (if schema rebuild needed)
                           regen-triggers
                           sql-quote
                           [test_command update]        (Step 2b if requested)

/tusk-init ─────────────── init --force
                           path
                           config
                           [test_command detection]     (Step 5b — detect from manifests, confirm)

/tusk-insights ─────────── (raw SQL queries only — no sub-commands)

```

---

## TASK DEVELOPMENT WORKFLOW (end-to-end)

```
USER / /loop
    │
    ├── /loop ──► tusk-loop.py
    │                │
    │                ├── v_ready_tasks + v_chain_heads (SQL)
    │                ├── claude -p /chain <id>   [chain head]
    │                └── claude -p /tusk  <id>   [standalone]
    │
    ▼
/tusk <id>  or  /chain <id>
    │
    ├── 1. task-start <id> --force
    │         └── opens/reuses session, sets In Progress
    │
    ├── 2. tusk branch <id> <slug>
    │         └── checkout default branch, pull, create feature/TASK-<id>-<slug>
    │
    ├── 3–5. [explore + implement]
    │
    ├── 6. PER CRITERION LOOP:
    │       commit <id> "<msg>" <files> --criteria <cid>
    │         ├── test_command gate     (hard-block; --skip-verify bypasses; exit 2 on failure)
    │         ├── lint                  (advisory)
    │         ├── git stage + commit    [TASK-<id>] format
    │         └── criteria done <cid>  (binds commit hash)
    │       progress <id> --next-steps "..."
    │
    ├── 7. criteria list <id>           (verify all done)
    │
    ├── 8. lint                         (advisory, standalone check)
    │
    ├── 9. /review-commits              (if mode = ai_only)
    │         ├── config
    │         ├── review start
    │         ├── [parallel reviewer agents]
    │         │       └── review add-comment / approve / request-changes
    │         ├── review list / status / resolve
    │         ├── review summary
    │         └── task-insert           (for deferred findings → /retro picks up)
    │
    ├── 10. FINALIZE:
    │       tusk merge <id> --session $SESSION_ID
    │         ├── session-close         (parses transcript, records session stats)
    │         ├── ff-merge feature branch → default branch + push
    │         ├── delete feature branch
    │         └── task-done --reason completed (returns unblocked_tasks[])
    │
    │       [tusk merge <id> --session $SESSION_ID --pr]  ← Ask path: opens PR,
    │         merges via gh pr merge --squash --delete-branch instead of local ff
    │
    └── 11. /retro
              ├── setup  (config + backlog)
              ├── [reads FOCUS.md if present]  (custom category override)
              ├── dupes check  (before each proposed insert)
              ├── task-insert  (follow-up tasks + lint-rule tasks for grep-detectable patterns)
              └── (conventions add --qualitative for qualitative patterns; lint-rule add for grep-detectable)
```

---

## CLI COMMAND CALL GRAPH (internal dependencies)

```
COMMAND             CALLS INTERNALLY
──────────────────  ─────────────────────────────────────────────────────
commit              test_command gate  ·  lint  ·  criteria done  (per --criteria flag)
                    exit codes: 2=test_command, 3=git add/commit, 4=criteria
merge               preflight checks  ·  session-close  ·  task-done  (+ git ff-merge, push, branch delete)
branch              (git operations only — no tusk sub-commands)
task-insert         dupes check  ·  wsjf
task-update         wsjf
task-reopen         regen-triggers             (resets validation triggers; DB state repair)
session-close       session-stats  ·  call-breakdown --session --write-only
skill-run finish    call-breakdown --skill-run --write-only
lint                (static rules 1–15)  ·  DB-backed rules 16–17 (from lint_rules table)
lint-rule           add / list / remove  (managed via tusk-lint-rules.py)
loop                claude -p /chain  |  claude -p /tusk
```

---

## COMMAND GROUPS

```
LIFECYCLE
  task-insert ──► task-start ──► [work] ──► task-done
  branch              create feature/TASK-<id>-<slug> off default branch
  merge               ff-merge + push + branch delete + task-done  (Ship path)
  merge --pr          open GitHub PR → gh pr merge --squash         (Ask path)
  task-update         (modify fields mid-flight)
  task-reopen         (reset stuck In Progress / Done → To Do)
  autoclose           (expire deferred, close moot contingent, close merged PRs)
  wsjf                (recompute priority_score for all open tasks)

WORK CAPTURE
  commit              test_command gate + lint + stage + commit + criteria done (atomic)
  criteria            add / list / done / skip / reset
  progress            append checkpoint to task_progress
  review              start / add-comment / resolve / approve / summary

SESSION & COST
  session-close ──► session-stats + call-breakdown
  session-stats       (standalone transcript parser)
  session-recalc      (bulk recompute after pricing.json changes)
  skill-run           start / finish ──► call-breakdown / list
  call-breakdown      --task | --session | --skill-run | --criterion

GRAPH & BLOCKING
  deps                add / remove / list / ready / blocked / all
  blockers            add / list / resolve / remove / blocked / all
  chain               scope / frontier / status  (sub-DAG queries)

OBSERVABILITY
  dashboard ──► dashboard-data / dashboard-html / dashboard-css / dashboard-js
  token-audit         (scans skill files for token anti-patterns)
  conventions         print / add  (add --qualitative for qualitative patterns; deprecated for grep-detectable — use lint-rule add)
  setup               config + backlog (one call)

CONFIG & SCHEMA
  init ──► migrate
  migrate             (apply pending schema migrations)
  regen-triggers      (drop + recreate validation triggers from config)
  validate            (check config.json against expected schema)
  upgrade ──► migrate
  config              (print resolved config JSON; includes test_command field)

UTILITY
  sql-quote           (safe string escaping for SQL interpolation)
  dupes               check / scan / similar
  lint                (static rules 1–15 + DB-backed rules 16–17, advisory)
  lint-rule           add / list / remove  (DB-backed grep rules; blocking=rule16, advisory=rule17)
  path                (print resolved DB path)
  shell               (interactive sqlite3)
  version             (print distribution version)
  pricing-update      (fetch latest Anthropic prices)
  sync-skills         (regenerate .claude/skills/ symlinks)
```

---

## SKILL × COMMAND MATRIX

```
                    task-  task-  task-  task-  merge    commit  crit-  review  deps  chain  dupes  setup  progress  wsjf  lint  config  conv-  skill-
SKILL               start  done   insert update                  eria                               entions       run
────────────────── ──────  ────── ────── ──────  ───────  ──────  ─────  ──────  ────  ─────  ─────  ─────  ────────  ────  ────  ──────  ─────  ──────
/tusk                 ✓      ✗             ✗       ✓        ✓       ✓             ✗            ✗      ✗        ✓        ✗     ✓      ✓       ✗       ✗
/chain†               ✓      ✗             ✗       ✗        ✗       ✓             ✗     ✓       ✗      ✗        ✓        ✗     ✓      ✗       ✗       ✗
/loop                 ✗      ✗             ✗       ✗        ✗       ✗             ✗            ✗      ✗        ✗        ✗     ✗      ✗       ✗       ✗
/review-commits       ✗      ✗      ✓      ✗       ✗        ✗       ✗      ✓      ✗     ✗       ✗      ✗        ✗        ✗     ✗      ✓       ✗       ✗
/retro                ✗      ✗      ✓      ✗       ✗        ✗       ✗      ✗      ✗     ✗       ✓      ✓        ✗        ✗     ✗      ✗       ✗       ✗
/groom-backlog        ✗      ✓      ✗      ✓       ✗        ✗       ✗      ✗      ✓     ✗       ✓      ✓        ✗        ✗     ✗      ✓       ✗       ✓
/create-task          ✗      ✗      ✓      ✗       ✗        ✗       ✗      ✗      ✗     ✗       ✓      ✓        ✗        ✗     ✗      ✓       ✗       ✗
/resume-task          ✓      ✗      ✗      ✗       ✗        ✗       ✓      ✗      ✗     ✗       ✗      ✗        ✓        ✗     ✓      ✗       ✗       ✗
/token-audit          ✗      ✗      ✓*     ✗       ✗        ✗       ✗      ✗      ✗     ✗       ✗      ✓        ✗        ✗     ✗      ✓       ✓       ✗
/tusk-update          ✗      ✗      ✗      ✗       ✗        ✗       ✗      ✗      ✗     ✗       ✗      ✗        ✗        ✗     ✗      ✓       ✗       ✗
/tusk-insights        ✗      ✗      ✗      ✗       ✗        ✗       ✗      ✗      ✗     ✗       ✗      ✗        ✗        ✗     ✗      ✗       ✗       ✗
/dashboard            ✗      ✗      ✗      ✗       ✗        ✗       ✗      ✗      ✗     ✗       ✗      ✗        ✗        ✗     ✗      ✗       ✗       ✗
/tusk-init            ✗      ✗      ✗      ✗       ✗        ✗       ✗      ✗      ✗     ✗       ✗      ✗        ✗        ✗     ✗      ✓       ✗       ✗

* via /create-task delegation
† /chain orchestrator calls chain scope/frontier/status and criteria done (deferred criteria) directly;
  all other ✓ entries are delegated to per-task /tusk sub-agents
  merge column = "tusk merge" (wraps session-close + task-done + git ff-merge + push + branch delete)
  conventions column: /retro uses conventions add --qualitative for qualitative patterns; lint-rule add / task-insert for grep-detectable
```

---

## BRANCH & MERGE MODEL

```
tusk branch <id> <slug>
    ├── detect default branch (remote HEAD → gh → "main")
    ├── stash dirty working tree (if any)
    ├── checkout default branch + pull
    ├── check for existing feature/TASK-<id>-* branch
    │     one found → warn + switch (skip creation)
    │     many found → error
    │     none found → git checkout -b feature/TASK-<id>-<slug>
    └── pop stash (with conflict detection)

tusk merge <id> --session <session_id>            ← Ship path (default)
    ├── session-close <session_id>                 (captures diff stats before branch deleted)
    ├── git merge --ff-only feature/TASK-<id>-* → default branch
    ├── git push origin <default-branch>
    ├── git branch -d feature/TASK-<id>-*
    └── task-done <id> --reason completed --force
          └── returns unblocked_tasks[]

tusk merge <id> --session <session_id> --pr       ← Ask path (CI / human review required)
    ├── session-close <session_id>
    ├── gh pr create --base <default-branch>
    └── gh pr merge --squash --delete-branch

MERGE PATH SELECTION (Ship / Show / Ask):
  Ship  →  tusk merge <id>        local ff-merge, no PR, instant
  Ask   →  tusk merge <id> --pr   open PR, merge via GitHub (CI, approvals, etc.)
```

---

## SHARED LIBRARY

```
tusk-pricing-lib.py  (not a command — imported by Python scripts)
    ├── imported by: session-stats, session-recalc, criteria, skill-run, call-breakdown
    └── provides: load_pricing(), aggregate_session(), compute_cost(),
                  compute_tokens_in(), iter_tool_call_costs(),
                  upsert_criterion_tool_stats(), find_transcript()
```
