# Trigger Smoke Test

Run this procedure after `tusk regen-triggers` whenever a trigger-validated field (`domains`, `task_types`, `statuses`, `priorities`, `closed_reasons`) was changed. Confirms that INSERT and UPDATE triggers reject invalid values and accept valid ones.

Pick the column that corresponds to the changed config key:

| Config key changed | Column to test |
|--------------------|----------------|
| `domains`          | `domain`       |
| `task_types`       | `task_type`    |
| `statuses`         | `status`       |
| `priorities`       | `priority`     |
| `closed_reasons`   | `closed_reason`|

Replace `<column>` with that column name and `<valid_value>` with a value that was **just added** to the config (prefer a newly-added value over a pre-existing default, to test the trigger against the actual change). Repeat Parts A–C for each modified field; run cleanup once after all fields are tested.

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
