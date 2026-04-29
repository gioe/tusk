# Migration Templates

Reference for writing schema migrations as `migrate_N(db_path, config_path, script_dir)` functions in `bin/tusk-migrate.py`. (`cmd_migrate()` in `bin/tusk` is a one-line dispatcher that execs the Python runner — all real migration logic lives in `bin/tusk-migrate.py`.)

> **Auto-regen of validation triggers.** `tusk migrate` calls the trigger-regen logic (`regen_triggers` in `bin/tusk-migrate.py`) as its final step, after all migrations have applied. The regen DROPs every `validate_*` trigger and recreates the set from the current `config.json`, so any new trigger coverage in `tusk-config-tools.py` and any drift introduced since the last migrate get installed without a separate `tusk regen-triggers` call. Because regen is idempotent and runs unconditionally, individual migrations no longer need to regenerate triggers themselves — keep the trigger-recreation steps below for completeness, but the final-step auto-regen is the safety net. Pairs with `tusk validate`'s trigger-drift detection.

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

---

## Adding a New Top-Level Config Key

When adding a new key to `config.default.json`, you must also register it in `KNOWN_KEYS` inside `bin/tusk-config-tools.py` (line ~34). Rule 7 of the config linter validates that every key in `config.default.json` is present in `KNOWN_KEYS` — missing it causes `tusk init` and `tusk validate` to fail.

**Checklist:**

1. Add the key to `config.default.json` with its default value.
2. Add the key name to the `KNOWN_KEYS` set in `bin/tusk-config-tools.py`.
3. Update `cmd_init()` in `bin/tusk` to handle the new key if it affects trigger generation or DB setup.
4. If the key drives enum validation (like `domains` or `task_types`), run `tusk regen-triggers` to rebuild the SQLite validation triggers from the updated config.
5. Update `CLAUDE.md`'s **Config-Driven Validation** section if the key has semantics worth documenting.
6. Bump `VERSION` and add a `CHANGELOG.md` entry — new config keys are distributed to target projects.

---

## Seeding Audit Tables: No Historical Recovery

When a new audit table (e.g. `task_status_transitions`, added in migration 53) is introduced to capture events that were never previously recorded, the migration may seed one or more synthetic rows per existing task to avoid an empty table. These seeds represent *only* what can be reconstructed from columns already in the DB (e.g. `tasks.started_at`, `tasks.closed_at`) — they are **not** an attempt to recover historical events that were never captured.

For example, `migrate_53` writes a synthetic `'To Do → In Progress'` row at `started_at` and a synthetic `'In Progress → Done'` row at `closed_at` for each completed task. Any intermediate reopens — `Done → To Do` via `tusk task-reopen --force`, or repeated `In Progress ↔ To Do` cycles — are permanently lost, because the database never stored them. The `task_metrics.reopen_count` column will therefore always be 0 for historical tasks; the value of the new audit table is forward-looking.

**Rule:** Document the seeded shape and the "no historical recovery" caveat in the migration's docstring and in `docs/DOMAIN.md`. Don't synthesize plausible-but-unknowable rows — a clean "we can't know" is better than a fabricated event history.
