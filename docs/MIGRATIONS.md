# Migration Templates

Reference for writing schema migrations inside `cmd_migrate()` in `bin/tusk`.

---

## Table-Recreation Migration

SQLite does not support `ALTER COLUMN` or `DROP COLUMN` (on older versions). Any migration that changes column constraints, renames a column, or removes a column requires recreating the table.

```bash
# Migration N→N+1: <describe what changed and why>
if [[ "$current" -lt <N+1> ]]; then
  sqlite3 "$DB_PATH" "
    BEGIN;

    -- 1. Drop validation triggers (they reference the table)
    $(sqlite3 "$DB_PATH" "SELECT 'DROP TRIGGER IF EXISTS ' || name || ';' FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'validate_%';")

    -- 2. Drop dependent views
    DROP VIEW IF EXISTS task_metrics;

    -- 3. Create the new table with the updated schema
    CREATE TABLE tasks_new (
        -- ... full column definitions with updated constraints ...
    );

    -- 4. Copy data from the old table
    INSERT INTO tasks_new SELECT * FROM tasks;
    --   If columns were added/removed/reordered, list them explicitly:
    --   INSERT INTO tasks_new (col1, col2, ...) SELECT col1, col2, ... FROM tasks;

    -- 5. Drop the old table
    DROP TABLE tasks;

    -- 6. Rename the new table
    ALTER TABLE tasks_new RENAME TO tasks;

    -- 7. Recreate any indexes that were on the original table
    --   (indexes are dropped automatically when the old table is dropped)

    -- 8. Recreate dependent views
    CREATE VIEW task_metrics AS
    SELECT t.*,
        COUNT(s.id) as session_count,
        SUM(s.duration_seconds) as total_duration_seconds,
        SUM(s.cost_dollars) as total_cost,
        SUM(s.tokens_in) as total_tokens_in,
        SUM(s.tokens_out) as total_tokens_out,
        SUM(s.lines_added) as total_lines_added,
        SUM(s.lines_removed) as total_lines_removed
    FROM tasks t
    LEFT JOIN task_sessions s ON t.id = s.task_id
    GROUP BY t.id;

    -- 9. Bump schema version
    PRAGMA user_version = <N+1>;

    COMMIT;
  "

  -- 10. Regenerate validation triggers from config
  local triggers
  triggers="$(generate_triggers)"
  if [[ -n "$triggers" ]]; then
    sqlite3 "$DB_PATH" "$triggers"
  fi

  # 11. Update DOMAIN.md to reflect new/modified tables, views, or triggers

  echo "  Migration <N+1>: <describe change>"
fi
```

**Key points:**

- Wrap the entire table-recreation DDL block inside an explicit `BEGIN;` / `COMMIT;` block within the `sqlite3` call. SQLite does not wrap multi-statement scripts in a single implicit transaction — each statement auto-commits independently. Without `BEGIN`/`COMMIT`, a kill between `DROP TABLE` and `ALTER TABLE ... RENAME` permanently destroys the original table. (This requirement applies to table-recreation migrations only; trigger-only migrations do not need `BEGIN`/`COMMIT`.)
- Steps 1 (drop triggers), 10 (regenerate triggers), and 11 (update DOMAIN.md) are separated: triggers are dropped inside the SQL transaction, regenerated afterward via the `generate_triggers` bash function, and DOMAIN.md is updated last as a manual step.
- Always update `PRAGMA user_version` inside the SQL block, and update the `tusk init` fresh-DB version to match.
- If the table has foreign keys pointing to it, SQLite will remap them automatically on `RENAME` as long as `PRAGMA foreign_keys` is OFF (the default for raw `sqlite3` calls).
- Test the migration on a copy of the database before merging: `cp tusk/tasks.db /tmp/test.db && TUSK_DB=/tmp/test.db tusk migrate`.

---

## Trigger-Only Migration

Some migrations only need to recreate validation triggers (e.g., after adding a new valid enum value to a config-driven column). These don't require table recreation, but they still need a version bump.

**Critical rule: bump `user_version` inside the same `sqlite3` call as trigger recreation — never before it.**

```bash
# Migration N→N+1: <describe what changed — e.g., add new domain value>
if [[ "$current" -lt <N+1> ]]; then
  local triggers
  triggers="$(generate_triggers)"
  sqlite3 "$DB_PATH" "
    -- 1. Drop existing validation triggers
    $(sqlite3 "$DB_PATH" "SELECT 'DROP TRIGGER IF EXISTS ' || name || ';' FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'validate_%';")

    -- 2. Recreate triggers with updated config
    $triggers

    -- 3. Bump schema version (MUST be in the same call as trigger recreation)
    PRAGMA user_version = <N+1>;
  "

  # 4. Update DOMAIN.md to reflect any schema or validation rule changes

  echo "  Migration <N+1>: <describe change>"
fi
```

**Why ordering matters:** If you bump `user_version` in a prior `sqlite3` call and the trigger recreation call subsequently fails, the DB is stuck at the new version with the trigger missing. Future `tusk migrate` runs will skip the migration while the trigger remains absent. Keep the version bump and trigger recreation atomic in the same call.
