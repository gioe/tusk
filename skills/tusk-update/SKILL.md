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
| `agents` | No | Free-form object; no DB triggers |
| `test_command` | No | Shell command run before each commit; empty string disables the gate |
| `dupes.strip_prefixes` | No | Python-side only |
| `dupes.check_threshold` | No | Python-side only (0.0–1.0) |
| `dupes.similar_threshold` | No | Python-side only (0.0–1.0) |

## Step 2b: Update test_command (if requested)

If the user explicitly asks to update `test_command`, run this step.

Read the current value:

```bash
tusk config test_command
```

Then scan the repo for test framework signals (check in this priority order):

1. `package.json` present → suggest `npm test`
2. `pyproject.toml` or `setup.py` present → suggest `pytest`
3. `Cargo.toml` present → suggest `cargo test`
4. `Makefile` present → check for a test target:
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

```bash
# Read current config
cat tusk/config.json
```

Use the Edit tool to update `tusk/config.json` with the new values. Preserve all fields — only modify the ones the user requested.

## Step 6: Regenerate Triggers (if needed)

If any trigger-validated field was changed (`domains`, `task_types`, `statuses`, `priorities`, `closed_reasons`), regenerate triggers:

```bash
tusk regen-triggers
```

This drops all existing `validate_*` triggers and recreates them from the updated config. **No data is lost.**

If only non-trigger fields changed (`agents`, `dupes`, `test_command`), skip this step.

## Step 7: Verify

Confirm the changes took effect:

```bash
tusk config
```

If trigger-validated fields were changed, run a quick smoke test:

```bash
# Verify new values are accepted (dry run — insert and immediately delete)
tusk "INSERT INTO tasks (summary, domain) VALUES ('__config_test__', 'new_domain')"
tusk "DELETE FROM tasks WHERE summary = '__config_test__'"
```

Report success to the user.

**Never call `tusk init --force`** — this destroys the database. Use `tusk regen-triggers` instead.
