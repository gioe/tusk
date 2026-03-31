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

Once you have a proposed domain, agent list, and `project_type`, proceed to **Step 3** and **Step 4** using these suggestions. In Step 3, substitute the user's stated plans as the evidence string (e.g., "planned React + FastAPI stack" instead of a scanned directory path).

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

- **add more** — proceed to the Pillar catalogue and Presentation sections below; insert only the new pillars the user confirms. **Before presenting the suggested list, filter out any pillar names already returned by `tusk pillars list`** — do not offer pillars that already exist. If all suggested pillars for the project_type are already present, skip presentation entirely and print:

  > All suggested pillars are already configured. Run `tusk pillars add` any time to add custom ones.

  Then proceed to Step 3b.
- **replace all** — delete every existing pillar with `tusk pillars delete --all`, then proceed through the full Presentation flow as normal.
- **skip** — print:

  > Pillars already configured — skipping. Run `tusk pillars add` any time to add more.

  and proceed to Step 3b.

**If the returned array is empty**, proceed to the Pillar catalogue and Presentation sections below as normal.

### Pillar catalogue (keyed by project_type)

Determine `project_type` by checking in order:
1. **Existing config** — run `tusk config project_type`; if it returns a non-empty value, use it.
2. **Step 2e interview result** — use the value derived during the fresh-project interview.
3. **Fallback** — treat as `null/other` if neither source yields a value.

Select the pre-populated set based on the resolved `project_type`:

| project_type | Suggested pillars |
|---|---|
| `web_app` | Performance, Accessibility, Reliability, Security, Maintainability |
| `ios_app` / `mobile` | Performance, Accessibility, Privacy, Reliability, Ergonomics |
| `python_service` / API | Reliability, Observability, Security, Performance, Maintainability |
| `cli_tool` | Ergonomics, Reliability, Portability, Efficiency, Transparency |
| `data_pipeline` / ML | Reliability, Data Integrity, Observability, Efficiency, Reproducibility |
| `library` | Ergonomics, Stability, Correctness, Portability |
| `docs_site` | Clarity, Discoverability, Accuracy, Maintainability |
| `monorepo` / `null` / other | Reliability, Maintainability, Security, Performance |

### Default core claims per pillar

Use these as the pre-populated core claim when presenting each pillar. The user can edit any claim before insertion.

| Pillar | Default core claim |
|---|---|
| Performance | The system responds quickly and uses resources efficiently |
| Accessibility | The product is usable by people of all abilities |
| Reliability | The system behaves correctly and recovers gracefully from failure |
| Security | User data and system resources are protected from unauthorized access |
| Maintainability | The codebase is easy to understand, change, and extend |
| Observability | The system's internal state is visible through logs, metrics, and traces |
| Privacy | User data is collected minimally and handled with care |
| Ergonomics | The interface feels natural and reduces cognitive load for its users |
| Portability | The system runs consistently across environments and platforms |
| Efficiency | The system accomplishes its goals with minimal waste of time or resources |
| Transparency | The system's behavior and reasoning are legible to its users |
| Stability | Public interfaces change rarely and only with clear migration paths |
| Correctness | The system produces accurate results that match its specification |
| Reproducibility | Given the same inputs, the system produces the same outputs every time |
| Data Integrity | Data is accurate, consistent, and never silently corrupted |
| Clarity | Content is easy to understand on first read |
| Discoverability | Users can find what they need without prior knowledge |
| Accuracy | Content reflects the current state of the system without gaps or errors |

### Presentation

Present the suggested list in a single message:

> **Design Pillars** — these guide tradeoff decisions throughout the project (e.g., "we chose X over Y because of *Reliability*").
>
> Suggested pillars for your project type:
>
> 1. **Performance** — "The system responds quickly and uses resources efficiently"
> 2. **Reliability** — "The system behaves correctly and recovers gracefully from failure"
> 3. ...
>
> Options: **confirm all** · **remove** (e.g., "remove 2") · **edit a claim** (e.g., "edit 1: new claim text") · **add** (e.g., "add Simplicity: ...") · **skip**

Wait for the user's response and apply their edits to the in-memory list before inserting anything.

### Insertion

For each confirmed pillar, run:

```bash
tusk pillars add --name "<name>" --claim "<core claim>"
```

If the user skips, print:

> Skipped design pillars — run `tusk pillars add` any time to add them later.

and proceed to Step 3b.

## Step 3b: Scaffold Domain Reviewer Prompts

After domains are confirmed in Step 3, run:

```bash
tusk scaffold-reviewer-prompts
```

This creates a stub `REVIEWER-PROMPT-<domain>.md` in `.claude/skills/review-commits/` for each configured domain. Each stub prepends a domain-specific focus comment to the base `REVIEWER-PROMPT.md` content. Existing files are left untouched (idempotent). The command exits 0 silently if the `review-commits` skill directory does not exist.

If the returned JSON `created` array is non-empty, print a one-line summary:

> Created N domain reviewer prompt(s): REVIEWER-PROMPT-api.md, REVIEWER-PROMPT-frontend.md, ...

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

Build the call using the values confirmed in Steps 3–5:

```bash
tusk init-write-config \
  --domains '<json_array_of_confirmed_domains>' \
  --agents '<json_object_of_confirmed_agents>' \
  --task-types '<json_array_of_confirmed_task_types>' \
  --test-command '<confirmed_test_command_or_empty_string>' \
  --project-type '<project_type_from_step_2e_or_empty_string>'
```

**Auto-populated `project_libs`:** When `--project-type` is a known built-in type (`ios_app` or `python_service`) and `--project-libs` is not passed, the command automatically merges the matching `project_libs` entry from `config.default.json` into the config, preserving any other existing `project_libs` entries. No extra flag is needed for the default libs. To override or extend, pass `--project-libs` explicitly — it takes full precedence over auto-population:

```bash
tusk init-write-config \
  ... \
  --project-type 'ios_app' \
  --project-libs '{"ios_app":{"repo":"my-org/my-libs","ref":"main"}}'
```

For example, if domains are `["api", "frontend"]`, agents are `{"backend": {"model": "sonnet"}}`, task types are `["bug", "feature", "docs"]`, test command is `pytest`, and project type is `python_service`:

```bash
tusk init-write-config \
  --domains '["api","frontend"]' \
  --agents '{"backend":{"model":"sonnet"}}' \
  --task-types '["bug","feature","docs"]' \
  --test-command 'pytest' \
  --project-type 'python_service'
```

This automatically sets `project_libs.python_service` from the built-in defaults. Step 8.5 will use this to seed bootstrap tasks.

Pass only the flags for values the user explicitly confirmed. Keys not passed are carried forward from the existing config unchanged. To clear `test_command`, pass `--test-command ''`. To set `project_type` to null, pass `--project-type ''`.

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
    { "name": "ios_app", "repo": "gioe/ios-libs", "tasks": [...], "error": null },
    { "name": "bad_lib", "repo": "owner/repo", "tasks": [], "error": "404: tusk-bootstrap.json not found" }
  ]
}
```

If `libs` is empty, skip this step silently.

For each lib entry:

- If `error` is non-null, print a one-line warning and skip:
  > Warning: could not fetch bootstrap for `<repo>` — <error>.
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

  Insert each selected task:

  ```bash
  tusk task-insert "<summary>" "<description>" \
    --priority <priority> \
    --task-type <task_type> \
    --complexity <complexity> \
    --criteria "<criterion1>" \
    --criteria "<criterion2>"
  ```

  If the task has a `migration_hints` array, append each hint as an additional `--criteria` argument prefixed with `[Migration]`:

  ```bash
  tusk task-insert "<summary>" "<description>" \
    --priority <priority> \
    --task-type <task_type> \
    --complexity <complexity> \
    --criteria "<criterion1>" \
    --criteria "<criterion2>" \
    --criteria "[Migration] <hint1>" \
    --criteria "[Migration] <hint2>"
  ```

  Tasks without a `migration_hints` field (or with an empty array) are seeded exactly as before — no `[Migration]` criteria are added.

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

- **Fresh project (no code)**: Skip Steps 2a–2d. Run the **Step 2e fresh-project interview** to collect domain/agent signals from the user's stated plans. After Steps 3–4 confirm the domain and agent list, direct the user to **Step 9** (seed from project description) as the primary task-seeding path — there are no TODOs to scan, so Step 9 is both the first and most important seeding route.
- **Monorepo** (`packages/*/` or `apps/*/`): One domain per package; let user trim.
- **>20 TODOs**: Summarize by file/category; let user pick which to seed.
