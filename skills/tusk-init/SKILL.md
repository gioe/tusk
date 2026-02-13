---
name: tusk-init
description: Interactive setup wizard to configure tusk for your project — scans codebase, suggests domains/agents, writes config, and optionally seeds tasks from TODOs
allowed-tools: Bash, Read, Write, Glob, Grep
---

# Tusk Init — Project Setup Wizard

Interactive config wizard that replaces manual `tusk/config.json` editing. Scans the codebase, suggests project-specific values, and writes the final config.

## Step 1: Check Existing Config

```bash
tusk config
```

- If config exists with **non-default values** (e.g., `domains` is non-empty or `agents` has keys), warn the user:
  > "You already have a customized tusk config. Reconfiguring will overwrite your current domains and agents, and `tusk init --force` will recreate the database (existing tasks will be lost). Do you want to proceed?"
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

| Signal | Suggested Domain |
|--------|-----------------|
| `src/components/`, `components/`, React/Vue/Angular/Svelte in deps | `"frontend"` |
| `src/api/`, `routes/`, `api/`, Express/FastAPI/Flask/Django/Rails in deps | `"api"` |
| `migrations/`, `prisma/`, `models/`, SQLAlchemy/TypeORM/Drizzle in deps | `"database"` |
| `infrastructure/`, `terraform/`, `.github/workflows/`, Pulumi/CDK in deps | `"infrastructure"` |
| `docs/` directory exists | `"docs"` |
| `packages/*/` or `apps/*/` (monorepo) | One domain per package/app name |
| React Native, Flutter, Swift, Kotlin in deps or config | `"mobile"` |
| PyTorch, TensorFlow, scikit-learn, pandas, numpy in deps | `"data"` or `"ml"` |
| CLI tools (commander, clap, cobra) in deps | `"cli"` |
| Auth-related dirs (`auth/`, `iam/`) or libs (passport, next-auth) | `"auth"` |

If **no signals are found** (fresh project with no code), skip scanning results and ask the user open-ended questions about their planned project structure.

## Step 3: Suggest and Confirm Domains

Present your findings with reasoning:

> Based on your project structure, I'd suggest these domains:
>
> - **frontend** — Found `src/components/` and React in package.json
> - **api** — Found `src/api/` with Express routes
> - **database** — Found `prisma/` schema and migrations
>
> Would you like to confirm these, add more, or remove any? (Leave empty to disable domain validation entirely.)

Wait for the user to confirm, add, remove, or skip. Store the final list.

## Step 4: Suggest and Confirm Agents

Based on the confirmed domains, suggest agent roles. Use this mapping as a starting point:

| Domain | Suggested Agent Key | Description |
|--------|-------------------|-------------|
| `frontend` | `"frontend"` | `"Handles UI components, styling, and client-side logic"` |
| `api` | `"backend"` | `"Handles API endpoints, business logic, and server-side code"` |
| `database` | `"backend"` | (merge with api if both exist) |
| `infrastructure` | `"infrastructure"` | `"Handles CI/CD, deployment, and infrastructure"` |
| `docs` | `"docs"` | `"Handles documentation and technical writing"` |
| `mobile` | `"mobile"` | `"Handles mobile app development"` |
| `data` / `ml` | `"data"` | `"Handles data pipelines and ML models"` |
| `cli` | `"cli"` | `"Handles CLI commands and tooling"` |

Always include a general-purpose agent if domains are specific:
- `"general"`: `"General-purpose development tasks"`

Present the suggestions and let the user confirm, modify, or skip (empty = no agent assignment validation).

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

Write the file:

```bash
# First check the path
tusk path
```

Then write `tusk/config.json` using the Write tool (NOT echo/cat).

After writing, reinitialize the database to apply new validation triggers:

```bash
tusk init --force
```

Print a summary:

> **Config written to `tusk/config.json`**
> - Domains: frontend, api, database
> - Agents: frontend, backend, general
> - Task types: bug, feature, refactor, test, docs, infrastructure
> - Database reinitialized with new validation triggers

## Step 7: CLAUDE.md Snippet

Check if the project has a `CLAUDE.md` and whether it already references tusk:

1. Use Glob for `CLAUDE.md` at the repo root
2. If it exists, Read it and search for `tusk` or `.claude/bin/tusk`
3. If tusk is already mentioned, skip this step and tell the user: "Your CLAUDE.md already references tusk."
4. If `CLAUDE.md` exists but doesn't mention tusk, offer to append:

> I can add a Task Queue section to your CLAUDE.md with tusk usage instructions. Would you like me to append it?

If the user confirms, append this snippet to the end of `CLAUDE.md`:

```markdown

## Task Queue

The project task database is managed via `tusk` (at `.claude/bin/tusk`). Use it for all task operations:

\```bash
.claude/bin/tusk "SELECT ..."          # Run SQL
.claude/bin/tusk -header -column "SQL"  # With formatting flags
.claude/bin/tusk path                   # Print resolved DB path
.claude/bin/tusk config                 # Print project config
.claude/bin/tusk init                   # Bootstrap DB (new projects)
.claude/bin/tusk shell                  # Interactive sqlite3 shell
.claude/bin/tusk version                # Print installed version
.claude/bin/tusk upgrade                # Upgrade from GitHub
\```

Never hardcode the DB path — always go through `tusk`.
```

5. If no `CLAUDE.md` exists, skip and mention: "No CLAUDE.md found — consider creating one for your project."

## Step 8: Seed Tasks from TODOs (Optional)

Scan the codebase for actionable comments:

```bash
# Use Grep to find TODO/FIXME/HACK/XXX comments
# Exclude: node_modules, .git, vendor, dist, build, tusk, __pycache__
```

Use Grep with these patterns (run in parallel):
- `TODO`
- `FIXME`
- `HACK`
- `XXX`

Exclude directories by filtering results: ignore any matches in `node_modules/`, `.git/`, `vendor/`, `dist/`, `build/`, `tusk/`, `__pycache__/`, `.venv/`, `target/`.

### If no TODOs found:
Skip this step silently. Do not mention it.

### If TODOs are found:
Present them grouped by file:

> I found **12 TODO/FIXME comments** across your codebase:
>
> **src/api/auth.ts** (3 items):
> - Line 42: `// TODO: Add rate limiting to login endpoint`
> - Line 87: `// FIXME: Token refresh race condition`
> - Line 103: `// TODO: Support OAuth providers`
>
> **src/components/Dashboard.tsx** (2 items):
> - Line 15: `// TODO: Add loading skeleton`
> - Line 89: `// HACK: Workaround for stale cache`
>
> Would you like me to create tasks from any of these? You can say "all", list specific ones, or "skip".

### For each TODO the user wants to keep:

1. Determine task properties:
   - **Summary**: Clean text from the comment (strip `TODO:`, `FIXME:`, etc. prefix)
   - **Domain**: Infer from file path using the configured domains
   - **Priority**: `"High"` for FIXME/HACK, `"Medium"` for TODO/XXX
   - **Task type**: `"bug"` for FIXME/HACK, `"feature"` for TODO/XXX

2. Check for duplicates before inserting:
   ```bash
   tusk dupes check "<summary>" --domain <domain>
   ```

3. If no duplicate (exit code 0), insert:
   ```bash
   tusk "INSERT INTO tasks (summary, description, status, priority, domain, task_type, created_at, updated_at)
     VALUES ('<summary>', 'Found in <file>:<line>

   Original comment: <full comment text>', 'To Do', '<priority>', '<domain>', '<task_type>', datetime('now'), datetime('now'))"
   ```

4. If duplicate found (exit code 1), skip and report: "Skipped — similar to existing task #N"

After all inserts, show a summary:

> **Seeded N tasks** from TODO comments (M skipped as duplicates).

## Edge Cases

- **Fresh project with no code**: Skip Step 2 scanning. Ask the user directly: "What are the main areas/modules of your project?" Use their answers to suggest domains.
- **Monorepo detected** (`packages/*/` or `apps/*/`): Suggest one domain per package. Present the list and let the user trim.
- **Reconfigure warning**: In Step 1, always warn about data loss from `tusk init --force` when config already has custom values. Offer to back up: `cp tusk/tasks.db tusk/tasks.db.bak`
- **No CLAUDE.md**: Skip Step 7 append. Mention the user should create one.
- **SQL injection in TODO text**: Always escape single quotes in TODO text before inserting (replace `'` with `''` in SQL strings).
- **Very large number of TODOs**: If more than 30 are found, show only the first 30 and mention the total count. Let the user choose which to seed.

## Important Guidelines

- All database access goes through the `tusk` CLI — never use raw `sqlite3`
- Always run `/check-dupes` before inserting tasks
- Wait for user confirmation at each decision point (Steps 3, 4, 5, 7, 8)
- Escape all user-derived strings in SQL to prevent injection
- Use parallel tool calls wherever possible for scanning (Step 2)
