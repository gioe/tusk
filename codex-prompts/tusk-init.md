# Tusk Init — Project Setup Wizard (Codex)

CLI-driven setup for `tusk/config.json`. The heavy lifting lives in the
`tusk init-wizard` subcommand (the Codex-facing port of the `/tusk-init`
Claude Code skill). This prompt orchestrates that command and the optional
task-seeding flows around it.

> **Conventions:** When you need a project convention (commits, structure,
> testing, migrations, skill authoring, etc.), run
> `tusk conventions search <topic>` instead of restating rules inline.
> Conventions are stored in the tusk DB and stay current — embedded text
> drifts.

## Step 1: Check for Existing Tasks

```bash
tusk "SELECT COUNT(*) FROM tasks;"
```

- **Non-zero count** — offer a backup before continuing:
  ```bash
  cp "$(tusk path)" "$(tusk path).bak"
  ```
  Warn the user that `tusk init --force` (which the wizard runs in Step 6)
  destroys all existing tasks. Stop if they decline.
- **Zero tasks** — proceed without warning.

## Step 2: Run the Wizard

`tusk init-wizard` performs Steps 2–6 of the original `/tusk-init` flow as a
single CLI call: it scans the codebase (`tusk init-scan-codebase`), detects
the test framework (`tusk test-detect`), prompts for any missing values when
stdin is a TTY, and writes the merged config via `tusk init-write-config`
(which atomically backs up the existing config and runs `tusk init --force`).

### 2a: Interactive (default when stdin is a TTY)

```bash
tusk init-wizard
```

The wizard asks one question at a time and confirms suggestions. Forward the
prompts to the user verbatim — do not pre-fill answers on their behalf.

### 2b: Non-interactive (scripting / CI)

Pass every value the user has already decided as a flag. Anything you omit
is carried forward from the existing config. Empty strings clear a value
(e.g., `--test-command ''` clears `test_command`).

```bash
tusk init-wizard --non-interactive \
  --domains '["api","frontend"]' \
  --agents '{"backend":"APIs and DB","frontend":"UI"}' \
  --task-types '["bug","feature","docs"]' \
  --test-command 'pytest -q' \
  --project-type python_service
```

> **Agents value shape:** `agents` is a JSON object mapping agent name →
> description **string**, not name → dict. Passing `{"backend":{"model":"sonnet"}}`
> fails validation.

### 2c: Domains, agents, project type

Suggested mappings (the wizard auto-derives these from codebase signals; use
them only if you need to advise the user during interactive mode):

| Signal in codebase / stated plans | Domain | Agent |
|---|---|---|
| React/Vue/Angular/Svelte/Next.js, `components/` | `frontend` | `frontend` |
| FastAPI/Django/Express/Flask, `api/` or `routes/` | `api` | `backend` |
| Prisma/migrations/ORM, `models/` | `database` | `backend` |
| Terraform/Docker/`.github/workflows/` | `infrastructure` | `infrastructure` |
| PyTorch/TensorFlow/pandas | `data` | `data` |
| React Native/Flutter/Swift/Kotlin | `mobile` | `mobile` |
| `docs/` directory | `docs` | `docs` |
| commander/clap/cobra | `cli` | `cli` |
| `packages/*/` or `apps/*/` (monorepo) | one domain per package | per domain |

Always include a `general` agent regardless of domain choices.

`project_type` (used by Step 4 below): `web_app`, `ios_app`, `cli_tool`,
`python_service`, `data_pipeline`, `docs_site`, `library`, `monorepo`, or
omit/empty for "other".

The wizard returns JSON on stdout. On `"success": false`, surface the
`error` field to the user and stop — the config is restored from backup if
one existed.

## Step 3: Pillars (Design Tradeoffs)

Pillars guide tradeoff decisions in this project. The wizard does not seed
them; do it as a follow-up.

```bash
tusk pillars list
```

- **Empty** — offer to add the standard set for the project type. Insert
  each one the user confirms with:
  ```bash
  tusk pillars add --name "<name>" --claim "<core claim>"
  ```
  If the user skips, print a one-line note that they can run `tusk pillars
  add` later and continue.
- **Non-empty** — show the existing list. Offer **add more**, **replace all**
  (`tusk pillars delete --all` first, then re-seed), or **skip**. When adding
  more, filter out any pillar names already present so you don't propose a
  duplicate.

## Step 4: Project-Lib Bootstrap Tasks (Optional)

`tusk init-write-config` auto-populates `project_libs` for known
`project_type` values (`ios_app`, `python_service`). Each lib ships its own
`tusk-bootstrap.json` with a curated task list.

```bash
tusk init-fetch-bootstrap
```

Returns:

```json
{
  "libs": [
    { "name": "ios_app", "repo": "gioe/ios-libs", "tasks": [...], "error": null },
    { "name": "bad_lib", "repo": "owner/repo", "tasks": [], "error": "404: ..." }
  ]
}
```

For each lib entry:

- `error` non-null — print a one-line warning and skip:
  > Warning: could not fetch bootstrap for `<repo>` — `<error>`.
- `error` null and `tasks` non-empty — present the task list, ask **yes /
  no / pick**, then insert each chosen task:
  ```bash
  tusk task-insert "<summary>" "<description>" \
    --priority <priority> \
    --task-type <task_type> \
    --complexity <complexity> \
    --criteria "<criterion1>" \
    --criteria "<criterion2>"
    # one --criteria flag per entry; if migration_hints is non-empty,
    # append each as: --criteria "[Migration] <hint>"
  ```

Track the bootstrap-seeded count for the final summary in Step 7.

## Step 5: Seed Tasks from TODOs (Optional)

```bash
tusk init-scan-todos
```

Scans the project root for `TODO`, `FIXME`, `HACK`, and `XXX` comments
(excluding `node_modules/`, `.git/`, `vendor/`, `dist/`, `build/`, `tusk/`,
`__pycache__/`, `.venv/`, `target/`). Returns a JSON array; each item has
`file`, `line`, `text`, `keyword`, `priority`, and `task_type`.

- Empty array — skip silently.
- Non-empty — offer **insert all / pick / skip**. When picking, present the
  list with file:line citations and let the user trim by index. Insert with
  `tusk task-insert`, propagating the suggested `priority` and `task_type`.
- More than ~20 items — group by file or category before presenting so the
  user isn't drowned in a flat list.

## Step 6: Seed Tasks from Project Description (Optional)

> Describe what you're building to create initial tasks? Good for fresh
> projects (no TODOs to scan yet).

If the user accepts, take their freeform description and follow the
`create-task.md` prompt's decomposition flow (Steps 3–8 there). When you
return, continue with Step 7.

## Step 7: AGENTS.md Snippet

`tusk init` (run inside `tusk init-wizard` Step 6) appends a task-tool
guidance block to `AGENTS.md` automatically — it's marked with the
`<!-- tusk-task-tools -->` sentinel and is idempotent. No manual edit
required.

If `AGENTS.md` does not exist, `tusk init` creates it. Confirm by running:

```bash
grep -c "tusk-task-tools" AGENTS.md
```

Should print `1`. If it prints `0`, surface this to the user — `tusk init`
likely failed silently and the wizard's JSON output should have an error to
investigate.

## Step 8: Finish

Show a final summary including:

- Confirmed domains, agents, task types, project_type, test_command
- DB location: `$(tusk path)`
- Pillar count (from Step 3)
- Total tasks seeded (Steps 4 + 5 + 6)

If any tasks were seeded, suggest next steps:

> **N tasks** added to your backlog.
>
> - `tusk wsjf` — see your backlog sorted by priority score
> - `tusk task-list --status "To Do"` — review what's queued
> - In a Codex session, work the next task with `tusk task-start` and follow
>   the `tusk.md` prompt.

If nothing was seeded, omit the next-steps block.

## Edge Cases

- **Fresh project (no code)** — the wizard's codebase scan returns empty
  detected_domains. In interactive mode it falls back to a short interview
  (project type, frameworks, areas of work). In non-interactive mode you
  must pass `--domains`, `--agents`, etc., explicitly. Direct the user to
  Step 6 (description-seeded tasks) as the primary seeding route — there
  are no TODOs to scan.
- **Monorepo (`packages/*/`, `apps/*/`)** — one domain per major
  sub-package; let the user trim. The wizard's scan handles this when
  `--auto-scan` is enabled (the default).
- **Re-running on a configured project** — the wizard merges only the keys
  you pass. The previous config is backed up to `tusk/config.json.bak`
  before each write; if `tusk init --force` fails, the backup is restored
  automatically.
