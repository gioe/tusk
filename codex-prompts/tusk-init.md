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
  --project-type python_service \
  --init-intent '{"audience":"Partner systems","primary_workflows":["ingest webhook"],"platforms":["backend"],"stack_preferences":["FastAPI"],"integrations":["Stripe"],"data_needs":["events"],"quality_priorities":["observability"],"launch_target":null,"non_goals":[],"open_questions":[],"project_type":"python_service"}' \
  --worktree-symlink-files '[".venv",".env"]'
```

> **Worktree symlink defaults:** `--worktree-symlink-files` is optional. When omitted alongside `--project-type python_service`, the helper seeds `[".venv", ".env"]` automatically; `--project-type ios_app` leaves the existing value untouched. Pass the flag explicitly when you need a non-default basename list (e.g. for a Node monorepo: `'["node_modules",".env.local"]'`).

> **Agents value shape:** `agents` is a JSON object mapping agent name →
> description **string**, not name → dict. Passing `{"backend":{"model":"sonnet"}}`
> fails validation.

> **Intent value shape:** `--init-intent` should be the normalized object from
> `tusk init-intent normalize`, not a transcript. The stable fields are
> `audience`, `primary_workflows`, `platforms`, `stack_preferences`,
> `integrations`, `data_needs`, `quality_priorities`, `launch_target`,
> `non_goals`, `open_questions`, and `project_type`.

### 2c: Fresh-project intent interview

When the wizard finds no codebase signals, it asks intent questions before
domain, agent, project type, or scaffold choices. If you need to advise the
user outside the interactive wizard, use the same helper contract:

```bash
tusk init-intent questions
tusk init-intent follow-ups --answers '<json object from answers so far>'
tusk init-intent normalize --answers '<json object>'
tusk init-intent archetype --answers '<normalized intent json>' --scan '<scan json when available>'
```

Ask the base questions first: audience/problem, first end-to-end workflows,
launch platforms, stack preferences, integrations, and quality priorities.
Then ask only the conditional follow-ups returned by `follow-ups`; do not
expand this into a long fixed questionnaire. Pass the normalized result to
`tusk init-wizard --init-intent` or include it in the eventual
`tusk init-write-config` call.

Before using inferred defaults, surface the archetype recommendation and its
rationale. Let the user accept it or override with a known ID:
`consumer_ios_app`, `internal_tool`, `b2b_dashboard`, `api_service`,
`content_site`, `library`, `data_pipeline`, `monorepo`, or `ambiguous`.
The override changes the default domains, agents, project type, pillar hints,
utility modules, and first vertical-slice task hints, but it must not rewrite
the normalized `init_intent` answers.

### 2d: Domains, agents, project type

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

`tusk init-write-config` auto-populates `project_libs` through
`tusk init-bootstrap-select` when `--project-libs` is not passed. Selection
uses the confirmed project type, normalized intent, inferred archetype,
platforms, and feature preferences. Today the built-in concrete packs are
`ios_app` (`gioe/ios-libs`) and `python_service` (`gioe/python-libs`);
future optional packs such as `android_app`, `web_app`, and `backend` are
reported as skipped when they match but have no repo configured yet.

```bash
tusk init-bootstrap-select \
  --project-type '<project_type>' \
  --intent '<normalized init_intent json>' \
  --archetype '<archetype json>'
```

Each selected lib ships its own `tusk-bootstrap.json` with a curated task
list and, for richer manifests, optional composable bootstrap modules.

```bash
tusk init-fetch-bootstrap
```

Returns:

```json
{
  "libs": [
    {
      "name": "ios_app",
      "repo": "gioe/ios-libs",
      "manifest_schema_version": 2,
      "tasks": [...],
      "modules": [...],
      "manifest_files": [...],
      "error": null
    },
    {
      "name": "bad_lib",
      "repo": "owner/repo",
      "manifest_schema_version": 1,
      "tasks": [],
      "modules": [],
      "manifest_files": [],
      "error": "404: ..."
    }
  ]
}
```

Before writing starter files or seeding bootstrap tasks, build a single
reviewable plan:

```bash
tusk init-bootstrap-plan \
  --picked '<confirmed init values json>' \
  --archetype '<inferred archetype json>' \
  --bootstrap '<init-fetch-bootstrap json>' \
  --scaffold-spec '<confirmed scaffold spec json>'
```

Show the user one plan containing the confirmed intent, inferred archetype,
selected utility repos, selected modules with matched reasons, skipped
optional modules, files to create or append, context atoms, pillars, glossary
entries, bootstrap tasks, generated first vertical-slice tasks, test-command
defaults, and worktree symlink suggestions. Generated vertical-slice tasks
connect the user's first workflow to behavior, data, integrations, tests, and
docs. Options are **accept**, **remove module**, **add module**, **pick tasks**,
**remove task**, **add/edit task**, and **skip materialization**. Rebuild the
plan with `--plan-remove-module <id>` or `--plan-add-module '<json object>'`
after module edits. Rebuild generated tasks with `--plan-task-mode pick` plus
repeatable `--plan-task-id <id>`, `--plan-remove-task <id>`, or
`--plan-add-task '<json object>'`; for edits, remove the generated task id and
add the edited replacement.

For non-interactive `tusk init-wizard` calls, materialization side effects such
as `--scaffold-spec`, `--seed-bootstrap-tasks all`, or
`--seed-plan-tasks all` require `--plan-action accept` or
`--plan-action skip-materialization`. Use `--plan-only` to inspect the plan
without mutating config, files, or tasks. Accepted plan tasks are inserted only
when the plan materializes and `--seed-plan-tasks all` is passed.

For an accepted plan, seed durable project memory before starter assets or
bootstrap tasks:

```bash
tusk init-apply-memory --plan '<confirmed bootstrap plan json>' --task-id <task_id>
```

This inserts context atoms with `source=agent_handoff`, adds design pillars,
and adds glossary entries. It also derives context from `init_intent`: audience
and workflows become `memory`, non-goals become `assumption`, open questions
become `question`, and quality priorities become `decision` context plus pillar
suggestions. Re-runs skip existing context/type/content, pillar names, and
glossary terms instead of duplicating them. `tusk init-wizard` applies this
when `--memory-task-id <id>` is passed with `--plan-action accept`; it does not
apply memory for `--plan-only` or `--plan-action skip-materialization`.

For each lib entry:

- `error` non-null — print a one-line warning and skip:
  > Warning: could not fetch bootstrap for `<repo>` — `<error>`.
- `error` null and `modules` non-empty — surface that module data was found.
  Modules are validated bootstrap-pack metadata for future planning: they can
  describe applicability rules, files, append operations, dependencies,
  pillars, glossary terms, context atoms, recommended tasks, and verification
  hints. Do not auto-apply module contents unless the current flow explicitly
  asks for module selection.
- `error` null and `manifest_files` non-empty — materialize only after the
  user accepts the reviewable plan. Use `tusk init-write-manifest-files` for
  all file writes. It supports safe create-only files, append-if-missing
  snippets, marker-bounded managed sections, `--dry-run` previews, and
  `--intent-file <json>` rendering for `{{ dotted.path }}` variables from the
  confirmed init intent. Surface the summary and any `conflicts`; conflicts
  mean the file was left unchanged and should be handled before task seeding.
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
