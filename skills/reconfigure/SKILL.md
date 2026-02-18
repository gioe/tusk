---
name: reconfigure
description: Update domains, agents, task types, and other config settings post-install without losing data
allowed-tools: Bash, Read, Write, Edit
---

# Reconfigure Skill

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

If the user specified what to change after `/reconfigure`, proceed with those changes. Otherwise, present the current config and ask what they'd like to update.

Configurable fields:

| Field | Requires Trigger Regen | Notes |
|-------|----------------------|-------|
| `domains` | Yes | Empty array disables validation |
| `task_types` | Yes | Empty array disables validation |
| `statuses` | Yes | Always validated; changing can break workflow queries |
| `priorities` | Yes | Always validated |
| `closed_reasons` | Yes | Always validated |
| `agents` | No | Free-form object; no DB triggers |
| `dupes.strip_prefixes` | No | Python-side only |
| `dupes.check_threshold` | No | Python-side only (0.0–1.0) |
| `dupes.similar_threshold` | No | Python-side only (0.0–1.0) |

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

If only non-trigger fields changed (`agents`, `dupes`), skip this step.

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

## Important Guidelines

- **Never call `tusk init --force`** — this destroys the database. Use `tusk regen-triggers` instead.
- Always check for affected tasks before removing validated values
- Always get user confirmation before writing config changes
- Preserve unmodified fields when editing config
- Use `$(tusk sql-quote "...")` for any user-provided or variable text in SQL statements
