---
name: tusk-init
description: Interactive setup wizard to configure tusk for your project — scans codebase, suggests domains/agents, writes config, and optionally seeds tasks from TODOs or project description
allowed-tools: Bash, Read, Write, Glob, Grep
---

# Tusk Init — Project Setup Wizard

Interactive config wizard. Scans the codebase, suggests project-specific values, writes the final config.

## Step 1: Check for Existing Tasks

```bash
tusk "SELECT COUNT(*) FROM tasks;"
```

- **Non-zero task count**: offer backup (`cp "$(tusk path)" "$(tusk path).bak"`), warn that `tusk init --force` destroys all existing tasks. Stop if user declines.
- **Zero tasks**: proceed without warning.

## Step 1.5: Detect / Initialize Git Repository

Tusk works best inside a git repository — branch-per-task, commit history, and hooks all assume one. `install.sh` no longer requires a git repo to run, so fresh projects may reach this skill without one.

Detect:

```bash
git rev-parse --show-toplevel >/dev/null 2>&1 && echo "git" || echo "no-git"
```

- **`git`** — proceed to Step 2 silently.
- **`no-git`** — ask the user a single yes/no:

  > No git repository detected at the project root. Tusk relies on git for branch-per-task tracking, commit hashing, and hooks. Run `git init` here?
  >
  > Options: **yes** (init now) · **no** (continue without git — branch/commit/merge features will be limited)

  - **yes** — run `git init` and continue to Step 2.
  - **no** — print a one-line warning and continue to Step 2: `Skipped git init — branch/commit features will fail until a repo exists.`

## Step 2: Scan the Codebase

Gather project context silently using parallel tool calls. Do not ask the user anything yet.

### 2a: Project manifests

Glob (parallel) and Read any that exist:

```
package.json
pyproject.toml, setup.py, setup.cfg
Cargo.toml
go.mod
Gemfile
pom.xml, build.gradle, build.gradle.kts
docker-compose.yml, Dockerfile
CLAUDE.md
Makefile
```

### 2b: Directory structure

Glob (parallel):

```
src/*/    app/*/    lib/*/    packages/*/    apps/*/
```

```bash
ls -1d */ 2>/dev/null | head -30
```

### 2c: Common directories

Glob (parallel) — presence signals domain:

```
components/ or src/components/     → frontend
api/ or routes/ or src/api/        → api
migrations/ or prisma/ or models/  → database
tests/ or __tests__/ or spec/      → tests
infrastructure/ or terraform/      → infra
  or .github/workflows/
docs/                              → docs
```

### 2d: Domain inference rules

```
frontend       — components dirs, React/Vue/Angular/Svelte in deps
api            — api/routes dirs, Express/FastAPI/Flask/Django/Rails in deps
database       — migrations/prisma/models dirs, ORM libs in deps
infrastructure — infrastructure/terraform dirs, CI workflows
docs           — docs/ directory exists
mobile         — React Native/Flutter/Swift/Kotlin signals
data / ml      — PyTorch/TensorFlow/scikit-learn/pandas in deps
cli            — CLI framework (commander/clap/cobra) in deps
auth           — auth dirs or auth libs (passport, next-auth)
monorepo       — packages/*/ or apps/*/ → one domain per package
```

No signals found (fresh project) → skip scanning, proceed to **Step 2e** below.

### 2e: Fresh-project interview (no codebase signals found)

Ask the user these three questions in a single message:

> **Setting up a fresh project — a few quick questions to suggest the right domains and agents:**
>
> 1. **What kind of project are you building?**
>    web app · mobile app · CLI tool · API / backend service · data pipeline / ML · documentation site · library / package · monorepo · other
>
> 2. **What languages or frameworks are you planning to use?**
>    (Free text — e.g., "React + FastAPI", "Go CLI", "Next.js + Prisma + TypeScript")
>
> 3. **Which areas of work do you expect? (pick all that apply)**
>    UI / frontend · backend / API · database · infrastructure / CI-CD · data / ML · mobile · docs · CLI · auth · other

Map answers to domain and agent suggestions using these rules. Evaluate all three answers together:

| Signal | Domain | Agent |
|---|---|---|
| web app · React · Vue · Angular · Svelte · Next.js · UI/frontend selected | `frontend` | `frontend` |
| API/backend service · FastAPI · Django · Express · Rails · Flask · backend selected | `api` | `backend` |
| Prisma · migrations · ORM · "database" selected | `database` | `backend` (merged with `api` if both present) |
| infrastructure · Terraform · Docker · CI/CD · GitHub Actions | `infrastructure` | `infrastructure` |
| data pipeline · ML · PyTorch · TensorFlow · pandas · scikit-learn | `data` | `data` |
| mobile app · React Native · Flutter · Swift · Kotlin | `mobile` | `mobile` |
| documentation site · "docs" selected | `docs` | `docs` |
| CLI tool · commander · clap · cobra · "CLI" selected | `cli` | `cli` |
| Next.js / monorepo with packages/* | one domain per major sub-package (infer from frameworks above) | per domain |

Always include `general` agent regardless of answers.

Also derive a `project_type` key from question 1 using this table and store it for Step 6:

| Answer | `project_type` |
|---|---|
| web app | `web_app` |
| mobile app | `ios_app` |
| CLI tool | `cli_tool` |
| API / backend service | `python_service` |
| data pipeline / ML | `data_pipeline` |
| documentation site | `docs_site` |
| library / package | `library` |
| monorepo | `monorepo` |
| other | `null` |

Once you have a proposed domain, agent list, and `project_type`, proceed to **Step 2f** for directory scaffolding, then to **Step 3** and **Step 4** using these suggestions. In Step 3, substitute the user's stated plans as the evidence string (e.g., "planned React + FastAPI stack" instead of a scanned directory path).

### 2f: Fresh-project directory scaffolding

**Run only when Step 2e was reached** — i.e., Step 2c/2d found no codebase signals. Existing-codebase paths skip this entire step; tusk does not propose directories or write stubs into projects that already have code.

Tusk does not run language-specific scaffolders (`npm init`, `cargo new`, `xcodegen`). It only creates the directory plus a `.gitkeep` and a per-directory routing stub (`CLAUDE.md` in Claude installs, `AGENTS.md` in Codex installs) so future agents know what each directory is for and which agent owns it.

Map the domains and agents confirmed in Step 2e to a directory layout using the table below. Each row produces one directory entry — combine entries from every row whose signal applies. Use plural/canonical names (`backend`, not `api-server-v1`).

| Domain (from 2e) | Directory | Purpose stub | Agent |
|---|---|---|---|
| `frontend` | `frontend` | UI / client-side sources | `frontend` |
| `api` | `backend` | API and service code | `backend` |
| `database` | (covered by `backend`) | — | — |
| `mobile` (iOS) | `ios` | iOS app sources (Swift, UIKit, SwiftUI) | `mobile` |
| `mobile` (Android) | `android` | Android app sources (Kotlin/Java) | `mobile` |
| `mobile` (cross-platform) | `mobile` | React Native / Flutter sources | `mobile` |
| `infrastructure` | `infra` | Terraform / Docker / CI configs | `infrastructure` |
| `data` / `ml` | `data` | Pipelines, notebooks, ML models | `data` |
| `docs` | `docs` | User-facing documentation | `docs` |
| `cli` | `cli` | CLI command sources | `cli` |

Present the proposed layout to the user as **a single batch**, not one directory at a time:

> **Proposed project skeleton:**
>
> - `frontend/` — UI / client-side sources (agent: `frontend`)
> - `backend/`  — API and service code (agent: `backend`)
> - `docs/`     — User-facing documentation (agent: `docs`)
>
> Options: **accept all** · **edit list** (specify which to keep, rename, or drop) · **skip** (no directories created)

- **accept all** — proceed to the scaffolding call below with the full list.
- **edit list** — incorporate the user's edits, then proceed.
- **skip** — print `Skipped directory scaffolding — run /tusk-init again later if you want it.` and proceed to Step 3.

Build a JSON spec from the confirmed list (one object per directory) and call `tusk init-scaffold`:

```bash
tusk init-scaffold --spec '[
  {"name": "frontend", "purpose": "UI / client-side sources",         "agent": "frontend"},
  {"name": "backend",  "purpose": "API and service code",             "agent": "backend"},
  {"name": "docs",     "purpose": "User-facing documentation",        "agent": "docs"}
]'
```

`init-scaffold` auto-detects the install mode (`.claude/` present → Claude / writes `CLAUDE.md`; `AGENTS.md` present → Codex / writes `AGENTS.md`). For each entry it creates the directory, writes a `.gitkeep`, and writes the routing-stub file. Existing directories that already contain files are skipped — your code is never overwritten.

The command returns JSON describing what was created and what was skipped:

```json
{
  "success": true,
  "mode": "claude",
  "created": [{"directory": "frontend", "stub": "frontend/CLAUDE.md", "files": [".gitkeep", "CLAUDE.md"]}],
  "skipped": []
}
```

Surface a one-line summary to the user (e.g., `Scaffolded 3 directories — frontend/, backend/, docs/`) and proceed to Step 3.

**Non-interactive opt-out:** Callers that want the rest of the wizard but no scaffolding (CI seeds, automation) should skip this step entirely — there is no scaffold prompt unless Step 2e was reached.

## Step 3: Suggest and Confirm Domains

Run the automated codebase scanner before presenting suggestions:

```bash
tusk init-scan-codebase
```

This returns JSON `{"manifests": [...], "detected_domains": [{"name": "...", "confidence": "high|medium|low", "signals": [...]}]}`.

- Use `detected_domains` as the basis for domain suggestions — each entry already includes a `confidence` level and the `signals` that triggered it.
- If `detected_domains` is empty (no codebase signals found), skip to **Step 2e** for the fresh-project interview instead of proceeding to domain confirmation.
- If the command fails or is unavailable, fall back to the inline scanning approach described in Steps 2a–2d.

Before presenting suggestions, frame the concept for the user:

> **What makes a good domain?**
>
> Domains should reflect **structural or functional areas of your codebase** — the answer to "what part of the system will change?". Good domains map to directories, subsystems, or product areas (e.g., `api`, `frontend`, `database`, `cli`). Avoid language-based names like `bash`, `python`, or `sql` — those describe *how* the code is written, not *what* it does, and they add no routing value since file patterns already encode language implicitly.
>
> **Three domain axes to consider:**
> - **Structural** — what layer or subsystem changes? (e.g., `cli`, `api`, `database`, `scheduler`)
> - **Functional / product area** — what user-facing capability does it serve? (e.g., `auth`, `billing`, `notifications`)
> - **Infrastructure** — how is the system operated? (e.g., `infrastructure`, `monitoring`, `deployment`)
>
> For example, in tusk itself, `cli` is a structural domain meaning "the bash dispatcher (`bin/tusk`)". It's not named `bash` — the language is irrelevant; the structure is what matters.

Present each as `- **name** — evidence found`. User confirms, adds, removes, or empties to disable validation.

## Step 3.5: Design Pillars

After domains are confirmed, offer to seed design pillars — the core values that guide tradeoff decisions in this project.

### Pre-check: existing pillars

Before presenting the catalogue, run:

```bash
tusk pillars list
```

**If the returned array is non-empty**, do NOT present the default catalogue as if starting fresh. Instead, display the existing pillars:

> **Design Pillars already configured:**
>
> 1. **Performance** — "The system responds quickly and uses resources efficiently"
> 2. **Reliability** — ...
>
> Options: **add more** · **replace all** (clears existing and starts fresh) · **skip**

- **add more** — proceed to the Presentation sub-step below and insert only the new pillars the user confirms. **Before presenting the suggested list, filter out any pillar names already returned by `tusk pillars list`** — do not offer pillars that already exist. If all suggested pillars for the project_type are already present, skip presentation entirely and print:

  > All suggested pillars are already configured. Run `tusk pillars add` any time to add custom ones.

  Then proceed to Step 3b.
- **replace all** — delete every existing pillar with `tusk pillars delete --all`, then proceed through the full Presentation sub-step as normal.
- **skip** — print:

  > Pillars already configured — skipping. Run `tusk pillars add` any time to add more.

  and proceed to Step 3b.

**If the returned array is empty**, proceed to the Presentation sub-step below.

### Presentation

Load the pillar catalogue, default core claims, and the user-facing presentation template (used only when the Pre-check directs you here):

```
Read file: <base_directory>/PILLARS.md
```

Follow the instructions there to resolve `project_type`, select the pre-populated pillar set, present it to the user, and apply any edits to the in-memory list before proceeding to Insertion.

### Insertion

For each confirmed pillar, run:

```bash
tusk pillars add --name "<name>" --claim "<core claim>"
```

If the user skips, print:

> Skipped design pillars — run `tusk pillars add` any time to add them later.

and proceed to Step 4.

## Step 4: Suggest and Confirm Agents

Map confirmed domains to agents:

```
frontend       → "frontend"       — UI, styling, client-side
api / database → "backend"        — API, business logic, data layer
infrastructure → "infrastructure" — CI/CD, deployment
docs           → "docs"           — documentation
mobile         → "mobile"         — mobile development
data / ml      → "data"           — data pipelines, ML
cli            → "cli"            — CLI commands and tooling
(always)       → "general"        — general-purpose tasks
```

User confirms, modifies, or skips (empty = no agent validation).

## Step 5: Confirm Task Types

> Default task types: `bug`, `feature`, `refactor`, `test`, `docs`, `infrastructure`
>
> Add or remove any? (Most projects keep defaults.)

## Step 5b: Detect and Confirm Test Command

Run the automated detector:

```bash
tusk test-detect
```

This inspects the repo root for lockfiles and returns JSON `{"command": "<cmd>", "confidence": "high|medium|low|none"}`.

- If `confidence` is `"none"` or `command` is `null`, no framework was detected.
- Otherwise, use `command` as the suggestion.
- If the command fails or is unavailable, fall back to asking the user directly.

If a suggestion was found, present it:

> Detected **`<suggested_command>`** as your test command (runs before every commit).
>
> Options:
> - **Confirm** — use `<suggested_command>`
> - **Override** — enter a custom command
> - **Skip** — leave test_command empty (no gate)

If no manifest signals were found, ask:

> No test framework detected. Enter a test command to run before each commit, or press Enter to skip.

Store the confirmed value (empty string if skipped) for Step 6.

## Step 6: Write Config and Initialize

Call `tusk init-write-config` with the values confirmed in the previous steps. This command reads the existing config, merges only the keys you provide (carrying forward everything else), backs up the config, writes the new file, runs `tusk init --force`, and restores the backup on failure — all atomically.

Example call using values confirmed in Steps 3–5 (domains `["api","frontend"]`, agents `{"backend":"APIs and database work"}`, task types `["bug","feature","docs"]`, test command `pytest`, project type `python_service`):

```bash
tusk init-write-config \
  --domains '["api","frontend"]' \
  --agents '{"backend":"APIs and database work"}' \
  --task-types '["bug","feature","docs"]' \
  --test-command 'pytest' \
  --project-type 'python_service'
```

> **Agents value shape:** the config validator requires `agents` to be a JSON object mapping agent name → **description string**, not name → dict. Passing `{"backend":{"model":"sonnet"}}` will fail validation with `"agents.backend" value must be a string (got dict: ...)`.

Pass only the flags for values the user explicitly confirmed; keys not passed are carried forward from the existing config unchanged. To clear `test_command`, pass `--test-command ''`. To set `project_type` to null, pass `--project-type ''`.

**Auto-populated `project_libs`:** When `--project-type` is a known built-in (`ios_app` or `python_service`) and `--project-libs` is not passed, the command merges the matching `project_libs` entry from `config.default.json` into the config (preserving any existing `project_libs` entries). Step 8.5 then uses this to seed bootstrap tasks. To override, pass `--project-libs '{"<type>":{"repo":"<owner>/<repo>","ref":"<ref>"}}'` — it takes full precedence over auto-population.

The command returns JSON: `{"success": true, "config_path": "...", "backed_up": true}` on success.

**On `"success": false`:** The `error` field contains the failure reason. The config is restored from backup if one existed.

1. Surface the error to the user:
   > **`tusk init --force` failed:** `<error>`
   >
   > - **Config**: restored to previous state (if a backup existed), or left as newly written (if no backup).
   > - Re-run `/tusk-init` once the error above is resolved.
2. Stop — do not proceed to Step 7.

**On `"success": true`:** Print summary: confirmed domains, agents, task types, DB reinitialized.

## Step 7: CLAUDE.md Snippet

1. Glob for `CLAUDE.md` at repo root
2. If exists, Read and search for `tusk` or `.claude/bin/tusk`
3. Already mentioned → skip: "Your CLAUDE.md already references tusk."
4. Exists but no mention → read and follow Step 7 from:
   ```
   Read file: <base_directory>/REFERENCE.md
   ```
5. No `CLAUDE.md` → skip: "No CLAUDE.md found — consider creating one."

## Step 8: Seed Tasks from TODOs (Optional)

Run:

```bash
tusk init-scan-todos
```

This scans the project root for `TODO`, `FIXME`, `HACK`, and `XXX` comments, excluding `node_modules/`, `.git/`, `vendor/`, `dist/`, `build/`, `tusk/`, `__pycache__/`, `.venv/`, and `target/`. It returns a JSON array where each item has `file`, `line`, `text`, `keyword`, `priority`, and `task_type`.

- Empty array → skip silently
- Items found → read and follow Step 8 from:
  ```
  Read file: <base_directory>/REFERENCE.md
  ```

## Step 8.5: Seed Tasks from Project Lib Bootstraps (Optional)

Fetch bootstrap data for all configured project libs in one call:

```bash
tusk init-fetch-bootstrap
```

This reads `project_libs` from config, fetches each lib's `tusk-bootstrap.json` from GitHub, validates required keys, and returns:

```json
{
  "libs": [
    { "name": "ios_app", "repo": "gioe/ios-libs", "tasks": [...], "manifest_files": [...], "error": null },
    { "name": "bad_lib", "repo": "owner/repo", "tasks": [], "manifest_files": [], "error": "404: tusk-bootstrap.json not found" }
  ]
}
```

If `libs` is empty, skip this step silently.

For each lib entry:

- If `error` is non-null, print a one-line warning and skip:
  > Warning: could not fetch bootstrap for `<repo>` — <error>.
- If `error` is null and `manifest_files` is non-empty, write the deterministic files first via:

  ```bash
  tusk init-write-manifest-files --spec '<json array of manifest_files>'
  ```

  This creates files that don't exist yet (`mode: create_only`, the default) and idempotently appends lines to existing files (`mode: append_if_missing`). The writer returns `{wrote: [...], skipped: [...], summary: "wrote N files, skipped M existing"}` — surface the `summary` line to the user before the seed-tasks prompt below.

- If `error` is null and `tasks` is non-empty, present the task list to the user:

  > **`<lib-name>` bootstrap tasks found** — `tusk-bootstrap.json` from `<owner>/<repo>` contains N tasks to help you set up <lib-name> integration:
  >
  > 1. [summary] (task_type, complexity)
  > 2. ...
  >
  > Seed all N tasks? (yes / no / pick)

  - **Yes** — insert all tasks with `tusk task-insert`
  - **No** — skip
  - **Pick** — list tasks individually; user selects which to insert

  Insert each selected task. Pass one `--criteria` flag per entry in the task's `criteria` array; if `migration_hints` is present and non-empty, append each hint as an additional `--criteria` prefixed with `[Migration] `. Tasks without `migration_hints` (or with an empty array) get only the regular criteria.

  ```bash
  tusk task-insert "<summary>" "<description>" \
    --priority <priority> \
    --task-type <task_type> \
    --complexity <complexity> \
    --criteria "<criterion1>" \
    --criteria "<criterion2>" \
    # append once per migration hint (omit entirely if none):
    --criteria "[Migration] <hint1>"
  ```

Track bootstrap-seeded task count separately; roll it into the total count reported in Step 10.

## Step 9: Seed Tasks from Project Description (Optional)

> Describe what you're building to create initial tasks? (Good for new projects or to complement TODO-seeded tasks.)

- Declines → proceed to Step 10
- Accepts → read and follow:
  ```
  Read file: <base_directory>/SEED-DESCRIPTION.md
  ```
  Then proceed to Step 10.

## Step 10: Finish

Show a final setup summary (confirmed domains, agents, task types, DB location, test command).

If any tasks were inserted during Steps 8 or 9 (track the count across both steps), also display:

> **N tasks** added to your backlog. Suggested next steps:
>
> - Run `tusk wsjf` to see your backlog sorted by priority score
> - Run `/chain` or `/loop` in a new session to start working through tasks autonomously

If no tasks were seeded during this run, omit the next-steps block — finish with the summary only.

## Edge Cases

- **Fresh project (no code)**: Skip Steps 2a–2d. Run the **Step 2e fresh-project interview** to collect domain/agent signals from the user's stated plans, then **Step 2f** to propose and (optionally) write the directory skeleton with per-directory `CLAUDE.md` / `AGENTS.md` routing stubs. After Steps 3–4 confirm the domain and agent list, direct the user to **Step 9** (seed from project description) as the primary task-seeding path — there are no TODOs to scan, so Step 9 is both the first and most important seeding route.
- **Monorepo** (`packages/*/` or `apps/*/`): One domain per package; let user trim.
- **>20 TODOs**: Summarize by file/category; let user pick which to seed.
