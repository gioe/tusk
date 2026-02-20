---
name: tusk-init
description: Interactive setup wizard to configure tusk for your project — scans codebase, suggests domains/agents, writes config, and optionally seeds tasks from TODOs or project description
allowed-tools: Bash, Read, Write, Glob, Grep
---

# Tusk Init — Project Setup Wizard

Interactive config wizard. Scans the codebase, suggests project-specific values, writes the final config.

## Step 1: Check Existing Config

```bash
tusk config
```

- **Non-default config** (non-empty domains or agents): offer backup (`cp "$(tusk path)" "$(tusk path).bak"`), warn that `tusk init --force` destroys existing tasks. Stop if user declines.
- **Fresh/default config**: proceed without warning.

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

No signals found (fresh project) → skip scanning, ask user about planned structure.

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

## Step 6: Write Config and Initialize

Assemble `tusk/config.json`, preserving unchanged defaults:

```json
{
  "domains": ["<confirmed>"],
  "task_types": ["<confirmed>"],
  "statuses": ["To Do", "In Progress", "Done"],
  "priorities": ["Highest", "High", "Medium", "Low", "Lowest"],
  "closed_reasons": ["completed", "expired", "wont_do", "duplicate"],
  "agents": { "<confirmed>" },
  "dupes": {
    "strip_prefixes": ["Deferred", "Enhancement", "Optional"],
    "check_threshold": 0.82,
    "similar_threshold": 0.6
  }
}
```

Write the file (resolve path via `tusk path`), then:

```bash
tusk init --force
```

Print summary: confirmed domains, agents, task types, DB reinitialized.

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

- Declines → finish with summary
- Accepts → read and follow:
  ```
  Read file: <base_directory>/SEED-DESCRIPTION.md
  ```

## Edge Cases

- **Fresh project (no code)**: Skip Step 2. Ask user about planned structure. Step 9 is the primary path.
- **Monorepo** (`packages/*/` or `apps/*/`): One domain per package; let user trim.
- **>20 TODOs**: Summarize by file/category; let user pick which to seed.
