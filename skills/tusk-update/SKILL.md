---
name: tusk-update
description: Update domains, agents, task types, and other config settings post-install without losing data
allowed-tools: Bash, Read, Write, Edit
---

# Tusk Update Skill

Updates the tusk configuration after initial setup. Modifies `tusk/config.json` and regenerates validation triggers **without destroying the database or existing tasks**.

## Step 1: Load Current Config

Read the current configuration and database state:

```bash
tusk config
```

Also check how many open tasks exist per domain (to inform safe removal):

```bash
tusk -header -column "
SELECT domain, COUNT(*) as task_count
FROM tasks
WHERE status <> 'Done'
GROUP BY domain
ORDER BY task_count DESC
"
```

## Step 2: Determine What to Change

If the user specified what to change after `/tusk-update`, proceed with those changes. Otherwise, present the current config and ask what they'd like to update.

Configurable fields:

| Field | Requires Trigger Regen | Notes |
|-------|----------------------|-------|
| `domains` | Yes | Empty array disables validation |
| `task_types` | Yes | Empty array disables validation |
| `statuses` | Yes | Always validated; changing can break workflow queries |
| `priorities` | Yes | Always validated |
| `closed_reasons` | Yes | Always validated |
| `agents` | No | `{ "<name>": "description" }` — see note below |
| `test_command` | No | Shell command run before each commit; empty string disables the gate |
| `dupes.strip_prefixes` | No | Python-side only |
| `dupes.check_threshold` | No | Python-side only (0.0–1.0) |
| `dupes.similar_threshold` | No | Python-side only (0.0–1.0) |
| `review.mode` | No | `"disabled"` or `"ai_only"`; config-side only |
| `review.max_passes` | No | Integer; max fix-and-re-review cycles; config-side only |
| `review.reviewers` | No | Array of `{name, description}` objects; config-side only |
| `review_categories` | Yes | Valid comment categories; empty array disables validation |
| `review_severities` | Yes | Valid severity levels; empty array disables validation |

**Agents object shape:** Each key is an agent name used for task assignment; each value is a plain string describing what that agent handles. Example:

```json
{
  "agents": {
    "backend": "API, business logic, data layer",
    "frontend": "UI components, styling, client-side"
  }
}
```

This is the same shape `tusk-init` generates and what `tusk config` outputs. The object is **not DB-validated** — no triggers enforce agent names. It exists so `/tusk` can suggest the right agent when picking a task. An empty object (`{}`) disables agent filtering.

## Step 2b: Update test_command (if requested)

If the user explicitly asks to update `test_command`, run this step.

Read the current value:

```bash
tusk config test_command
```

Then scan the repo for test framework signals (check in this priority order):

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

1. `bun.lockb` or `bun.lock` present → run **Inspect package.json for test runner**, then:
   - No result → suggest `bun test`
   - `vitest` → suggest `bun run vitest`
   - `jest` → suggest `bun run jest`
2. `pnpm-lock.yaml` present → run **Inspect package.json for test runner**, then:
   - No result → suggest `pnpm test`
   - `vitest` → suggest `pnpm vitest`
   - `jest` → suggest `pnpm jest`
3. `package.json` present (no lockfile) → run **Inspect package.json for test runner**, then:
   - No result → suggest `npm test`
   - `vitest` → suggest `npx vitest`
   - `jest` → suggest `npx jest`
4. `pyproject.toml` or `setup.py` present → suggest `pytest`
5. `Cargo.toml` present → suggest `cargo test`
6. `Makefile` present → check for a test target:
   ```bash
   grep -q "^test:" Makefile && echo "has_test_target"
   ```
   If `has_test_target` → suggest `make test`

Present the current value and suggestion together:

> Current `test_command`: **`<current value>`** *(empty = no gate)*
>
> Auto-detected: **`<suggested_command>`** *(or "none detected")*
>
> Options:
> - **Keep current** — no change
> - **Use detected** — set to `<suggested_command>` *(only shown when a suggestion was found AND it differs from the current value)*
> - **Override** — enter a custom command
> - **Clear** — remove the gate (set to empty string)

Store the confirmed value. If the user chose "Keep current", skip the write for this field. Otherwise include it when writing config in Step 5.

## Step 3: Safety Checks for Removals

Before removing any value from a trigger-validated field (`domains`, `task_types`, `statuses`, `priorities`, `closed_reasons`), check if existing tasks use that value:

```bash
# Example: check if domain "old_domain" is in use
tusk -header -column "
SELECT id, summary, status FROM tasks
WHERE domain = 'old_domain' AND status <> 'Done'
"
```

If tasks use a value being removed:
1. **Show the affected tasks** to the user
2. **Offer migration options:**
   - Reassign to a different value (e.g., move tasks from domain `old` to domain `new`)
   - Close the tasks first
   - Cancel the removal
3. **Do not proceed** until the user confirms a migration path
4. **Execute the migration** before updating config:
   ```bash
   tusk "UPDATE tasks SET domain = 'new_value', updated_at = datetime('now') WHERE domain = 'old_value'"
   ```

For Done tasks referencing removed values: these won't cause trigger issues (triggers only fire on INSERT/UPDATE), but warn the user that historical data will reference the old value.

## Step 4: Present Changes for Confirmation

Show a clear diff of what will change:

```
Current config:
  domains: [cli, skills, schema, install, docs, dashboard]

Proposed config:
  domains: [cli, skills, schema, install, docs, dashboard, testing]
                                                            ^^^^^^^
  Added: testing
```

**Wait for explicit user confirmation before writing.**

## Step 5: Write Updated Config

Read the current config file, apply changes, and write it back:

Use the Read tool to load `tusk/config.json`, then use the Edit tool to update it with the new values. Preserve all fields — only modify the ones the user requested.

## Step 5b: Offer Task Reassignment for New Domains

**Only run this step if one or more domains were added in this update.**

After writing the config, check whether any open tasks have no domain assigned — these are natural candidates for the new domain:

```bash
tusk -header -column "
SELECT id, summary, task_type, priority
FROM tasks
WHERE status <> 'Done'
AND (domain IS NULL OR domain = '')
ORDER BY priority_score DESC, id
"
```

If the query returns **no rows**, skip this step silently.

If rows are returned, display them and prompt the user:

> **N open task(s) have no domain assigned. Would you like to reassign any to `<new_domain>`?**
>
> - **Reassign all** — set `domain = '<new_domain>'` for every listed task
> - **Pick specific tasks** — user provides a comma-separated list of IDs
> - **Skip** — leave domain assignments unchanged

If the user chooses **Reassign all**:

```bash
DOMAIN=$(tusk sql-quote "<new_domain>")
tusk "UPDATE tasks SET domain = $DOMAIN, updated_at = datetime('now') WHERE status <> 'Done' AND (domain IS NULL OR domain = '')"
tusk "SELECT changes() AS rows_updated"
```

If the user picks **specific IDs** (e.g., 12, 15, 18):

```bash
DOMAIN=$(tusk sql-quote "<new_domain>")
tusk "UPDATE tasks SET domain = $DOMAIN, updated_at = datetime('now') WHERE id IN (12, 15, 18) AND status <> 'Done'"
tusk "SELECT changes() AS rows_updated"
```

Report the `rows_updated` count to the user, then proceed to Step 6.

If the user chooses **Skip**, proceed to Step 6 without any changes. This is always safe — triggers are not affected by unassigned domains.

If multiple domains were added in this update, repeat this step for each new domain (showing a header like `--- Reassignment for domain: <new_domain> (1 of 2) ---`) before proceeding to Step 6.

## Step 6: Regenerate Triggers (if needed)

If any trigger-validated field was changed (`domains`, `task_types`, `statuses`, `priorities`, `closed_reasons`), regenerate triggers:

```bash
tusk regen-triggers
```

This drops all existing `validate_*` triggers and recreates them from the updated config. **No data is lost.**

If only non-trigger fields changed (`agents`, `dupes`, `test_command`, `review`), skip this step.

## Step 7: Verify

Confirm the changes took effect:

```bash
tusk config
```

If trigger-validated fields were changed, run a two-part smoke test for each modified field. Pick the `tasks` column that corresponds to the config key that was changed:

| Config key changed | Column to test |
|--------------------|----------------|
| `domains`          | `domain`       |
| `task_types`       | `task_type`    |
| `statuses`         | `status`       |
| `priorities`       | `priority`     |
| `closed_reasons`   | `closed_reason`|

Replace `<column>` with that column name and `<valid_value>` with a value that was **just added** to the config (prefer a newly-added value over a pre-existing default, to test the trigger against the actual change). Repeat the two-part test for each field that was modified; run cleanup once after all fields are tested.

> **Note:** `review_categories` and `review_severities` apply to the `review_comments` table, which requires a `review_id` foreign key. Skip the INSERT smoke test for those fields — the absence of errors from `tusk regen-triggers` is sufficient confirmation.

**Part A — Invalid value must be rejected** (core trigger check):

```bash
tusk "INSERT INTO tasks (summary, <column>) VALUES ('__tusk_trigger_smoke_test__', '__invalid__')"
```

Expected: non-zero exit with a trigger error. If this INSERT **succeeds**, the trigger is not working — report failure.

**Part B — Valid value must be accepted**:

```bash
tusk "INSERT INTO tasks (summary, <column>) VALUES ('__tusk_trigger_smoke_test__', '<valid_value>')"
```

Expected: zero exit. If this INSERT **fails**, the trigger is over-blocking valid values — report failure.

**Part C — UPDATE trigger: invalid value must be rejected, valid value must be accepted** (run only if Part B succeeded):

```bash
tusk "UPDATE tasks SET <column> = '__invalid__' WHERE summary = '__tusk_trigger_smoke_test__'"
```

Expected: non-zero exit with a trigger error. If this UPDATE **succeeds**, the UPDATE trigger is not working — report failure.

```bash
tusk "UPDATE tasks SET <column> = '<valid_value>' WHERE summary = '__tusk_trigger_smoke_test__'"
```

Expected: zero exit. If this UPDATE **fails**, the UPDATE trigger is over-blocking valid values — report failure.

> **Note:** Part C reuses the row inserted in Part B. If Part B failed (no row exists), these UPDATE commands will match 0 rows and succeed silently without firing the trigger — skip reporting Part C results in that case and rely on the Part B failure report.
>
> **Note (status column only):** When `<column>` is `status`, updating to `'__invalid__'` fires both `validate_status_update` (value validation) and `validate_status_transition` (transition validation). A non-zero exit confirms the trigger stack rejected the value but does not isolate which trigger fired. If `validate_status_update` were missing, the transition trigger would still catch it. This is acceptable — the combined rejection is the meaningful signal.

**Cleanup (always run, even if Part A, Part B, or Part C failed)**:

```bash
tusk "DELETE FROM tasks WHERE summary = '__tusk_trigger_smoke_test__'"
```

Report success to the user only if Part A rejected the invalid value, Part B accepted the valid value, and Part C rejected the invalid UPDATE while accepting the valid UPDATE.

**Never call `tusk init --force`** — this destroys the database. Use `tusk regen-triggers` instead.

## Step 8: Final Validation

Run `tusk validate` as the canonical final check after all writes and trigger regens:

```bash
tusk validate
```

- If `tusk validate` **fails**: show the full output to the user and warn that the configuration or database may have issues.
- If `tusk validate` **passes**: report "✓ Configuration updated and validated successfully."
