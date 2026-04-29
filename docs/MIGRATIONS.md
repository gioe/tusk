# Migration Templates

Reference for writing schema migrations as `migrate_N(db_path, config_path, script_dir)` functions in `bin/tusk-migrate.py`. (`cmd_migrate()` in `bin/tusk` is a one-line dispatcher that execs the Python runner — all real migration logic lives in `bin/tusk-migrate.py`.)

> **Auto-regen of validation triggers.** `tusk migrate` calls the trigger-regen logic (`regen_triggers` in `bin/tusk-migrate.py`) as its final step, after all migrations have applied. The regen DROPs every `validate_*` trigger and recreates the set from the current `config.json`, so any new trigger coverage in `tusk-config-tools.py` and any drift introduced since the last migrate get installed without a separate `tusk regen-triggers` call. Because regen is idempotent and runs unconditionally, individual migrations no longer need to regenerate triggers themselves — the templates below drop validation triggers inside the table-recreation transaction (since they reference the table being recreated) and rely on the final-step auto-regen to recreate them. Older migrations in `bin/tusk-migrate.py` keep their explicit `regen_triggers()` calls, which are idempotent and harmless. Pairs with `tusk validate`'s trigger-drift detection.

---

## Table-Recreation Migration

SQLite does not support `ALTER COLUMN` or `DROP COLUMN` (on older versions). Any migration that changes column constraints, renames a column, or removes a column requires recreating the table. Add the function to `bin/tusk-migrate.py` and register it in the `MIGRATIONS` list near the bottom of that file.

```python
def migrate_N(db_path: str, config_path: str, script_dir: str) -> None:
    """<one-line summary of what changed and why>

    <Optional longer rationale: links to the issue/task that motivated the
    change, edge cases, idempotency notes. Mirror the docstring style of the
    other migrate_X functions in bin/tusk-migrate.py.>

    Idempotent: <how a re-run on an already-migrated DB is a no-op — typically
    the get_version() guard at the top of the function.>
    """
    if get_version(db_path) >= N:
        _progress("  Migration N: <one-line summary>")
        return

    drop_triggers = drop_validate_triggers(db_path)

    run_script(db_path, f"""
        BEGIN;

        -- 1. Drop validation triggers (they reference the table being recreated).
        {drop_triggers}

        -- 2. Drop every view that projects columns from the table being
        --    recreated. SQLite freezes ``SELECT t.*`` column lists at CREATE
        --    VIEW time, so views must be DROPped and re-CREATEd to pick up
        --    column additions, renames, or removals. The currently-affected
        --    set is task_metrics, v_ready_tasks, v_chain_heads, and
        --    v_criteria_coverage — drop whichever of these reference the
        --    table you are recreating.
        DROP VIEW IF EXISTS task_metrics;
        DROP VIEW IF EXISTS v_ready_tasks;
        DROP VIEW IF EXISTS v_chain_heads;
        DROP VIEW IF EXISTS v_criteria_coverage;

        -- 3. Create the new table with the updated schema.
        CREATE TABLE tasks_new (
            -- ... full column definitions with updated constraints ...
        );

        -- 4. Copy data from the old table. If columns were added, removed, or
        --    reordered, list them explicitly on both sides:
        INSERT INTO tasks_new (col1, col2, ...) SELECT col1, col2, ... FROM tasks;

        -- 5. Drop the old table.
        DROP TABLE tasks;

        -- 6. Rename the new table.
        ALTER TABLE tasks_new RENAME TO tasks;

        -- 7. Recreate any indexes that were on the original table.
        --    (Indexes are dropped automatically when the old table is dropped.)

        -- 8. Recreate dependent views — copy each CREATE VIEW statement
        --    verbatim from cmd_init in bin/tusk so migrated DBs match fresh
        --    installs bit-for-bit. See migrate_56 in bin/tusk-migrate.py for
        --    a full worked example of view recreation.
        CREATE VIEW task_metrics AS
        SELECT t.*,
            COUNT(s.id) as session_count,
            -- ... see cmd_init for the full canonical column list ...
        FROM tasks t
        LEFT JOIN task_sessions s ON t.id = s.task_id
        GROUP BY t.id;

        -- (... recreate v_ready_tasks, v_chain_heads, v_criteria_coverage too ...)

        -- 9. Bump schema version.
        PRAGMA user_version = N;

        COMMIT;
    """)

    # 10. Update DOMAIN.md to reflect new/modified tables, views, or triggers.
    #
    # Validation triggers do NOT need to be recreated here — the auto-regen
    # note at the top of this file describes how tusk migrate's final-step
    # regen_triggers() rebuilds the entire validate_* set from the current
    # config.json after every migration run. New migrations should drop
    # validation triggers (step 1) but rely on the final-step auto-regen
    # to recreate them. Older migrations in bin/tusk-migrate.py still call
    # regen_triggers() explicitly; the call is idempotent and harmless.

    _progress("  Migration N: <one-line summary>")
```

**Key points:**

- The `BEGIN;` / `COMMIT;` block is required. SQLite does not wrap multi-statement scripts in a single implicit transaction — each statement auto-commits independently. Without `BEGIN`/`COMMIT`, a kill between `DROP TABLE` and `ALTER TABLE ... RENAME` permanently destroys the original table.
- Validation triggers are **dropped** inside the SQL transaction (because they reference the table being recreated) but are **not** recreated by the migration itself. The final-step `regen_triggers()` call in `main()` of `bin/tusk-migrate.py` (see the auto-regen note at the top of this file) recreates the full `validate_*` set from the current `config.json` after every migrate run, so an explicit per-migration regen call is no longer required.
- Always update `PRAGMA user_version` inside the SQL block, and bump the fresh-DB stamp in `cmd_init()` of `bin/tusk` to match. See the migration-N checklist in `CLAUDE.md` for the full set of files to touch.
- If the table has foreign keys pointing to it, SQLite will remap them automatically on `RENAME` as long as `PRAGMA foreign_keys` is OFF (the default for the `sqlite3.Connection` opened by `db_connect()` / `run_script()`).
- Test the migration on a copy of the database before merging: `cp tusk/tasks.db /tmp/test.db && TUSK_DB=/tmp/test.db tusk migrate`.

---

## Trigger-Only Migration

Most "trigger-only" migrations are no longer needed at all. The auto-regen note at the top of this file describes how `tusk migrate` regenerates every `validate_*` trigger from the current `config.json` as its final step on every invocation. So a config-driven enum addition (e.g., a new domain value, a new task_type) is picked up without any migration: edit `config.default.json` and run `tusk migrate`.

You only need a true trigger-only migration when you must **bump `user_version`** for an unrelated reason — e.g., to fence off a downstream behavior change, or to mark a config-schema break that cannot be inferred from the trigger set alone. In that case:

```python
def migrate_N(db_path: str, config_path: str, script_dir: str) -> None:
    """<one-line summary — e.g., bump version to fence off behavior change X>"""
    if get_version(db_path) >= N:
        _progress("  Migration N: <one-line summary>")
        return

    set_version(db_path, N)

    # Validation triggers are regenerated by tusk migrate's final-step
    # regen_triggers() call (see the auto-regen note at the top of this file)
    # — no explicit per-migration regen required.

    # Update DOMAIN.md if the change has documentation consequences.

    _progress("  Migration N: <one-line summary>")
```

The previous "bump `user_version` and recreate triggers in the same `sqlite3` call" hazard is gone, because trigger recreation now lives outside the per-migration scope (in `main()`'s final-step auto-regen). If your migration also needs a small DDL change (e.g. `ALTER TABLE ... ADD COLUMN`), see `migrate_60` in `bin/tusk-migrate.py` for a worked `run_script()` pattern that bundles the DDL with the version bump in a single transaction.

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
