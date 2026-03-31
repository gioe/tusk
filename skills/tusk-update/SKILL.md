---
name: tusk-update
description: Update domains, agents, task types, and other config settings post-install without losing data
allowed-tools: Bash, Read, Write, Edit
---

# Tusk Update Skill

Updates the tusk configuration after initial setup. Modifies `tusk/config.json` and regenerates validation triggers **without destroying the database or existing tasks**.

## Step 1: Silent Reconnaissance

Run all diagnostics without prompting the user. Collect everything needed to form recommendations in one pass.

**1a. Current config and domain task counts:**

```bash
tusk config
```

```bash
tusk -header -column "
SELECT domain, COUNT(*) as task_count
FROM tasks
WHERE status <> 'Done'
GROUP BY domain
ORDER BY task_count DESC
"
```

**1b. CLAUDE.md convention scan:**

```bash
ls CLAUDE.md 2>/dev/null && echo "exists" || echo "not found"
```

If CLAUDE.md exists, scan for any section whose heading contains the word "convention" (case-insensitive). Extract bullet points (lines beginning with `-` or `*`). If no matching section is found, or if the section contains only a pointer line (e.g., "Run `tusk conventions list`..."), there is nothing to migrate.

**1c. Existing conventions:**

```bash
tusk conventions list
```

Compare against CLAUDE.md bullets to identify which bullets are not already in the DB. These are migration candidates.

**1d. Pillars:**

```bash
tusk pillars list
```

**1e. Test command detection:**

```bash
tusk test-detect
```

This inspects the repo root for lockfiles and returns JSON `{"command": "<cmd>", "confidence": "high|medium|low|none"}`.

**1f. Unassigned tasks:**

```bash
tusk -header -column "
SELECT id, summary, task_type, priority
FROM tasks
WHERE status <> 'Done'
AND (domain IS NULL OR domain = '')
ORDER BY priority_score DESC, id
"
```

Hold all findings in memory. Do not output anything to the user yet.

## Step 2: Recommendations

Analyze the findings from Step 1 and form a prioritized, numbered recommendation list. Each entry must include what was found, what action would be taken, and why.

**Findings that trigger a recommendation:**

| Finding | Recommendation | Justification |
|---------|---------------|---------------|
| CLAUDE.md convention bullets not in DB | Migrate all bullets and replace section with pointer line | DB-stored conventions are queryable by topic; CLAUDE.md bullets are not |
| `test_command` is empty and `test-detect` returned a high/medium-confidence command | Set `test_command` to the detected command | Enables pre-commit test gate |
| `test_command` is set but differs from auto-detected command (high confidence) | Update `test_command` to the detected command | Detected command better matches current project tooling |
| Pillars list is empty | Note: no pillars configured; suggest running `/tusk-init` to seed them | Pillars provide design vocabulary for task evaluation |
| N open tasks have no domain assigned and the user has requested adding a new domain | Reassign N unassigned tasks to `<new_domain>` | Keeps the backlog organized without manual follow-up |
| User invoked `/tusk-update <description of changes>` | Include the user-requested changes as explicit recommendation items | User intent overrides auto-detection |

> **Note on low/none confidence:** If `test-detect` returns `low` or `none` confidence, no `test_command` recommendation is generated — auto-detection could not identify the project's test tooling with sufficient certainty. To set or clear `test_command` in this case, describe the change explicitly when invoking the skill (e.g., `/tusk-update set test_command to pytest tests/`).

**If the user specified changes** when invoking `/tusk-update` (e.g., `/tusk-update add a testing domain`), include those as top-priority items in the recommendation list alongside any auto-detected findings.

**Present the full recommendation list** in one message. Format each item as:

```
[N] <Action>
    Finding: <what was found>
    Why: <justification>
```

After the list, include the configurable fields reference table so the user knows what is changeable:

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
| `project_type` | No | String key identifying the project type (e.g. `python_service`, `ios_app`); `null` if unset |
| `project_libs.*.ref` | No | Pin a project lib's bootstrap ref to a tag or commit SHA; defaults to `"main"` |

**Agents object shape:** Each key is an agent name used for task assignment; each value is a plain string describing what that agent handles. Example:

```json
{
  "agents": {
    "backend": "API, business logic, data layer",
    "frontend": "UI components, styling, client-side"
  }
}
```

Then ask a single approval question:

> Which of these would you like to apply? Enter numbers (e.g. `1,3`), `all`, `none`, or describe additional changes:

**If there are no recommendations and the user requested no changes:** Report "Config looks healthy — no recommendations." and exit. Do not proceed further.

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

**Convention migration (if approved in Step 2):** For each approved convention bullet, run:

```bash
tusk conventions add "<bullet text (with formatting stripped)>"
```

Strip markdown emphasis markers (`**`, `*`, backtick fences) from the text before inserting — plain text is stored in the DB.

Then replace the conventions section in `CLAUDE.md` with a single pointer line using the Edit tool:

> Replace the "Key Conventions" section body with: `Run \`tusk conventions list\` to see project conventions.`

Leave the section heading in place.

## Step 5b: Execute Task Reassignment (if approved)

**Only run this step if a domain reassignment was approved in Step 2.**

Execute the reassignment without additional prompting. If the user approved reassigning all unassigned tasks to a new domain:

```bash
DOMAIN=$(tusk sql-quote "<new_domain>")
tusk "UPDATE tasks SET domain = $DOMAIN, updated_at = datetime('now') WHERE status <> 'Done' AND (domain IS NULL OR domain = '')"
tusk "SELECT changes() AS rows_updated"
```

If the user approved reassigning specific task IDs (e.g., 12, 15, 18):

```bash
DOMAIN=$(tusk sql-quote "<new_domain>")
tusk "UPDATE tasks SET domain = $DOMAIN, updated_at = datetime('now') WHERE id IN (12, 15, 18) AND status <> 'Done'"
tusk "SELECT changes() AS rows_updated"
```

Report the `rows_updated` count and proceed to Step 6.

If multiple domains were added, execute reassignment for each new domain in sequence.

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

## Step 8: Add a Project Lib (optional)

If the user wants to add a project lib that is not yet in `project_libs`, or if Step 1 reveals unconfigured built-in libs, run this step.

**8a. Identify unconfigured built-in libs:**

Built-in libs are defined in `config.default.json` under `project_libs`. Check which ones are not yet configured in the project's `tusk/config.json`:

```bash
tusk config project_libs
```

Compare against the built-in keys (`ios_app`, `python_service`). Any key absent from the project config is an unconfigured built-in.

**8b. Present and confirm:**

List unconfigured libs to the user. Example:

> The following built-in libs are available but not yet configured:
> - `ios_app` (gioe/ios-libs) — SharedKit UI design tokens, APIClient HTTP client
> - `python_service` (gioe/python-libs) — structured logging, OpenTelemetry/Sentry observability
>
> Would you like to add any of these? You can also provide a custom `--repo owner/repo`.

Wait for the user to select one or more libs (or decline).

**8c. Add the lib and seed bootstrap tasks:**

For each selected lib, call:

```bash
tusk add-lib --lib <name>
# or for a custom lib:
tusk add-lib --lib <name> --repo <owner/repo> [--ref <branch|tag|sha>]
```

This writes the lib to `project_libs` in `tusk/config.json` (no DB reinit) and fetches the bootstrap task list.

Parse the JSON output. If `error` is non-null, report it and skip seeding. Otherwise, present the fetched tasks to the user using the same pattern as `/tusk-init` Step 8.5:

> **Available bootstrap tasks for `<lib>`:**
> 1. `<summary>` (complexity: XS, priority: High)
> 2. `<summary>` (complexity: S, priority: Medium)
> ...
>
> Seed all tasks? Enter numbers (e.g. `1,3`), `all`, or `none`:

For each confirmed task, insert it:

```bash
tusk task-insert "<summary>" "<description>" \
  --priority <priority> \
  --task-type <task_type> \
  --complexity <complexity> \
  --domain <appropriate_domain> \
  $(for criterion in <criteria_array>; do echo "--criteria \"$criterion\""; done)
```

Report the created task IDs to the user.

## Step 9: Final Validation

Run `tusk validate` as the canonical final check after all writes and trigger regens:

```bash
tusk validate
```

- If `tusk validate` **fails**: show the full output to the user and warn that the configuration or database may have issues.
- If `tusk validate` **passes**: report "✓ Configuration updated and validated successfully."
