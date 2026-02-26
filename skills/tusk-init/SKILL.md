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

Once you have a proposed domain and agent list, proceed to **Step 3** and **Step 4** using these suggestions. In Step 3, substitute the user's stated plans as the evidence string (e.g., "planned React + FastAPI stack" instead of a scanned directory path).

## Step 3: Suggest and Confirm Domains

Present each as `- **name** — evidence found`. User confirms, adds, removes, or empties to disable validation.

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

Use the manifests found in Step 2a to suggest a test command. Check in this priority order:

**Sub-step – Inspect package.json for test runner** (used by items 1–3 below):
Run from repo root:
```bash
node -e "const p=require('./package.json'); console.log(JSON.stringify({scripts:p.scripts||{},dev:Object.keys({...p.devDependencies,...p.dependencies})}));" 2>/dev/null
```
Interpret the output:
- No output (no `package.json`, or `node` unavailable) → no result
- `vitest` appears in `devDependencies` or `dependencies` → result: `vitest`
- `jest` appears in `devDependencies`/`dependencies` OR a `test` script contains `jest` → result: `jest`
- Otherwise → no result

1. `bun.lockb` or `bun.lock` found → run **Inspect package.json for test runner**, then:
   - No result → suggest `bun test`
   - `vitest` → suggest `bun run vitest`
   - `jest` → suggest `bun run jest`
2. `pnpm-lock.yaml` found → run **Inspect package.json for test runner**, then:
   - No result → suggest `pnpm test`
   - `vitest` → suggest `pnpm vitest`
   - `jest` → suggest `pnpm jest`
3. `package.json` found (no lockfile) → run **Inspect package.json for test runner**, then:
   - No result → suggest `npm test`
   - `vitest` → suggest `npx vitest`
   - `jest` → suggest `npx jest`
4. `pyproject.toml` or `setup.py` found → suggest `pytest`
5. `Cargo.toml` found → suggest `cargo test`
6. `Makefile` found → check if it has a test target:
   ```bash
   grep -q "^test:" Makefile && echo "has_test_target"
   ```
   If `has_test_target` → suggest `make test`

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

Read the existing config first to preserve any custom review settings:

```bash
cat "$(tusk path | xargs dirname)/config.json" 2>/dev/null
```

Assemble `tusk/config.json`, carrying forward values from the existing config for any key the user has not explicitly changed (do not reset to defaults):

```json
{
  "domains": ["<confirmed>"],
  "task_types": ["<confirmed>"],
  "statuses": ["To Do", "In Progress", "Done"],
  "priorities": ["Highest", "High", "Medium", "Low", "Lowest"],
  "closed_reasons": ["completed", "expired", "wont_do", "duplicate"],
  "complexity": ["XS", "S", "M", "L", "XL"],
  "blocker_types": ["data", "approval", "infra", "external"],
  "criterion_types": ["manual", "code", "test", "file"],
  "agents": { "<confirmed>" },
  "dupes": {
    "strip_prefixes": ["Deferred", "Enhancement", "Optional"],
    "check_threshold": 0.82,
    "similar_threshold": 0.6
  },
  "review": {
    "mode": "<from existing config, or \"disabled\" if none>",
    "max_passes": 2,
    "reviewers": "<from existing config, or [] if none>"
  },
  "review_categories": ["must_fix", "suggest", "defer"],
  "review_severities": ["critical", "major", "minor"],
  "merge": {
    "mode": "<from existing config, or \"local\" if none>"
  },
  "test_command": "<confirmed or empty>"
}
```

For any top-level key the user has not explicitly changed in this wizard, read the value from the existing config and carry it forward — only use the defaults shown above if no existing config is present.

Before writing the new config, back it up unconditionally (covers both zero-task and non-zero-task cases):

```bash
CONFIG_DIR=$(tusk path | xargs dirname)
DB_PATH=$(tusk path)
[ -f "${CONFIG_DIR}/config.json" ] && cp "${CONFIG_DIR}/config.json" "${CONFIG_DIR}/config.json.bak"
```

Write the file (resolve path via `tusk path`), then run init and check the exit code:

```bash
tusk init --force
```

**On non-zero exit (failure):**

1. Restore backups for both config and DB (if available):
   ```bash
   [ -f "${CONFIG_DIR}/config.json.bak" ] && cp "${CONFIG_DIR}/config.json.bak" "${CONFIG_DIR}/config.json" && echo "Config restored from backup."
   [ -f "${DB_PATH}.bak" ] && cp "${DB_PATH}.bak" "${DB_PATH}" && echo "DB restored from backup."
   ```
2. Inform the user:
   > **`tusk init --force` failed.** The database may be in an inconsistent state.
   >
   > - **Config**: restored to previous state (if a backup existed), or left as newly written (if no backup).
   > - **DB** (`${DB_PATH}`): restored from backup if one existed, otherwise in unknown state. Re-run `/tusk-init` once the error above is resolved.
3. Stop — do not proceed to Step 7.

**On success:** Print summary: confirmed domains, agents, task types, DB reinitialized.

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

Grep (parallel): `TODO`, `FIXME`, `HACK`, `XXX`

Exclude: `node_modules/`, `.git/`, `vendor/`, `dist/`, `build/`, `tusk/`, `__pycache__/`, `.venv/`, `target/`

- No TODOs → skip silently
- TODOs found → read and follow Step 8 from:
  ```
  Read file: <base_directory>/REFERENCE.md
  ```

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
