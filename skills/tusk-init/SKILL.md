---
name: tusk-init
description: Interactive setup wizard to configure tusk for your project — scans codebase, suggests domains/agents, writes config, and optionally seeds tasks from TODOs or project description
allowed-tools: Bash, Read, Write, Glob, Grep
---

# Tusk Init — Project Setup Wizard

Interactive config wizard that replaces manual `tusk/config.json` editing. Scans the codebase, suggests project-specific values, and writes the final config.

## Step 1: Check Existing Config

```bash
tusk config
```

- If config exists with **non-default values** (e.g., `domains` is non-empty or `agents` has keys), offer to back up first (`cp "$(tusk path)" "$(tusk path).bak"`), then warn:
  > "Reconfiguring will overwrite domains/agents, and `tusk init --force` recreates the database (existing tasks lost). Proceed?"
- If the user declines, stop here.
- If config is fresh/default (empty domains, empty agents), proceed directly without warning.

## Step 2: Scan the Codebase

Gather project context using parallel tool calls. Do NOT ask the user anything yet — just collect signals silently.

### 2a: Detect project manifest files

Use Glob to check for these files (run in parallel):
- `package.json`
- `pyproject.toml`, `setup.py`, `setup.cfg`
- `Cargo.toml`
- `go.mod`
- `Gemfile`
- `pom.xml`, `build.gradle`, `build.gradle.kts`
- `docker-compose.yml`, `Dockerfile`
- `CLAUDE.md`

Read any that exist to extract tech stack info (framework names, dependencies).

### 2b: Detect directory structure

Use Glob for these patterns (run in parallel):
- `src/*/` — top-level source subdivisions
- `app/*/` — app directory subdivisions
- `lib/*/` — library subdivisions
- `packages/*/` — monorepo packages
- `apps/*/` — monorepo apps

Also run:
```bash
ls -1d */ 2>/dev/null | head -30
```
to see top-level directories.

### 2c: Check for common directories

Use Glob to check existence of (run in parallel):
- `src/components/**` or `components/**` — frontend signal
- `src/api/**` or `api/**` or `routes/**` — API signal
- `migrations/**` or `prisma/**` or `models/**` — database signal
- `tests/**` or `__tests__/**` or `spec/**` or `test/**` — test signal
- `infrastructure/**` or `terraform/**` or `.github/workflows/**` — infra signal
- `docs/**` — docs signal

### 2d: Apply tech stack inference rules

Based on what you found, build a list of suggested domains using these rules:

- `"frontend"` — components dirs, React/Vue/Angular/Svelte in deps
- `"api"` — api/routes dirs, Express/FastAPI/Flask/Django/Rails in deps
- `"database"` — migrations/prisma/models dirs, ORM libs in deps
- `"infrastructure"` — infrastructure/terraform dirs, CI workflows
- `"docs"` — docs/ directory exists
- `"mobile"` — React Native/Flutter/Swift/Kotlin signals
- `"data"` / `"ml"` — PyTorch/TensorFlow/scikit-learn/pandas in deps
- `"cli"` — CLI framework (commander/clap/cobra) in deps
- `"auth"` — auth dirs or auth libs (passport, next-auth)
- Monorepo (`packages/*/`, `apps/*/`) — one domain per package/app

If **no signals are found** (fresh project with no code), skip scanning results and ask the user open-ended questions about their planned project structure.

## Step 3: Suggest and Confirm Domains

Present each suggested domain as `- **name** — evidence found` (one line per domain). Ask the user to confirm, add, remove, or leave empty to disable domain validation. Wait for confirmation before proceeding.

## Step 4: Suggest and Confirm Agents

Based on the confirmed domains, suggest agent roles:

- `frontend` → `"frontend"` — UI components, styling, client-side logic
- `api` / `database` → `"backend"` — API endpoints, business logic, data layer (merge if both exist)
- `infrastructure` → `"infrastructure"` — CI/CD, deployment, infra
- `docs` → `"docs"` — documentation and technical writing
- `mobile` → `"mobile"` — mobile app development
- `data` / `ml` → `"data"` — data pipelines and ML models
- `cli` → `"cli"` — CLI commands and tooling
- Always include `"general"` — general-purpose development tasks

Let the user confirm, modify, or skip (empty = no agent validation).

## Step 5: Confirm Task Types

Show the current defaults:

> The default task types are: `bug`, `feature`, `refactor`, `test`, `docs`, `infrastructure`
>
> Would you like to add or remove any? (Most projects keep the defaults.)

This should be a quick confirm — most users will accept the defaults.

## Step 6: Write Config and Initialize

Assemble the final `tusk/config.json`. Preserve defaults for fields the user didn't change:

```json
{
  "domains": ["<confirmed domains>"],
  "task_types": ["<confirmed task_types>"],
  "statuses": ["To Do", "In Progress", "Done"],
  "priorities": ["Highest", "High", "Medium", "Low", "Lowest"],
  "closed_reasons": ["completed", "expired", "wont_do", "duplicate"],
  "agents": { "<confirmed agents>" },
  "dupes": {
    "strip_prefixes": ["Deferred", "Enhancement", "Optional"],
    "check_threshold": 0.82,
    "similar_threshold": 0.6
  }
}
```

Write `tusk/config.json` (resolve path via `tusk path` first), then reinitialize:

```bash
tusk init --force
```

Print a summary listing the confirmed domains, agents, task types, and confirmation that the database was reinitialized.

## Step 7: CLAUDE.md Snippet

Check if the project has a `CLAUDE.md` at the repo root:

1. Use Glob for `CLAUDE.md` at the repo root
2. If it exists, Read it and search for `tusk` or `.claude/bin/tusk`
3. If tusk is already mentioned, skip this step: "Your CLAUDE.md already references tusk."
4. If `CLAUDE.md` exists but doesn't mention tusk, read the reference file for the append workflow:

   ```
   Read file: <base_directory>/REFERENCE.md
   ```

   Follow the Step 7 instructions from the reference.

5. If no `CLAUDE.md` exists, skip and mention: "No CLAUDE.md found — consider creating one for your project."

## Step 8: Seed Tasks from TODOs (Optional)

Scan the codebase for actionable comments using Grep with these patterns (run in parallel): `TODO`, `FIXME`, `HACK`, `XXX`

Exclude directories by filtering results: ignore any matches in `node_modules/`, `.git/`, `vendor/`, `dist/`, `build/`, `tusk/`, `__pycache__/`, `.venv/`, `target/`.

If no TODOs are found, skip this step silently.

If TODOs are found, read the reference file for the task-seeding workflow:

```
Read file: <base_directory>/REFERENCE.md
```

Follow the Step 8 instructions from the reference.

## Step 9: Seed Tasks from Project Description (Optional)

Offer to create initial tasks from a high-level project description — particularly valuable for greenfield projects or when no TODOs were found in Step 8.

> Would you like to describe what you're building so I can create some initial tasks? This works well for new projects or to complement the TODO-seeded tasks.

If the user declines, finish the wizard with a summary.

If the user accepts, read the companion file for the interactive seeding workflow:

```
Read file: <base_directory>/SEED-DESCRIPTION.md
```

Follow its instructions.

## Edge Cases

- **Fresh project with no code**: Skip Step 2 scanning. Ask the user directly what areas/modules they plan. Step 9 (project description seeding) is the primary task-creation path for these projects.
- **Monorepo detected** (`packages/*/` or `apps/*/`): Suggest one domain per package. Let the user trim.
- **Large TODO count** (>20 matches in Step 8): Summarize by file/category and let the user pick which to seed rather than proposing all.
