---
name: criteria
description: Manage acceptance criteria for tasks (add, list, complete, reset)
allowed-tools: Bash
---

# Criteria Skill

Manages per-task acceptance criteria in the project task database (via `tusk` CLI). Acceptance criteria define the conditions that must be met for a task to be considered done.

## Commands

### Add a criterion

```bash
tusk criteria add <task_id> "criterion text" [--source original|subsumption|pr_review] [--type manual|code|test|file] [--spec "verification spec"]
```

The `--source` flag tracks where the criterion came from (default: `original`):
- **`original`** — defined when the task was created
- **`subsumption`** — inherited from a subsumed duplicate task
- **`pr_review`** — added during PR review

The `--type` flag sets the criterion type (default: `manual`):
- **`manual`** — verified by human judgment (no automated check)
- **`code`** — verified by running a shell command (exit code 0 = pass)
- **`test`** — verified by running a test command (exit code 0 = pass)
- **`file`** — verified by checking that file(s) matching a glob exist

The `--spec` flag provides the verification spec (required for non-manual types):
- For `code`/`test`: a shell command to run
- For `file`: a file path or glob pattern

Example:
```bash
tusk criteria add 42 "API returns 200 with valid payload" --source original
tusk criteria add 42 "Unit tests pass" --type test --spec "pytest tests/test_auth.py"
tusk criteria add 42 "Config file exists" --type file --spec "config/*.json"
```

### List criteria for a task

```bash
tusk criteria list <task_id>
```

Shows ID, completion status, type, source, cost, and criterion text.

### Mark a criterion as done

```bash
tusk criteria done <criterion_id> [--skip-verify]
```

For non-manual criteria, runs automated verification before marking done. If verification fails, the criterion is not marked done (exit code 1). Use `--skip-verify` to bypass verification.

### Reset a criterion to incomplete

```bash
tusk criteria reset <criterion_id>
```

Clears completion status, cost data, and verification result.

## Arguments

Parse the user's request to determine:
1. The subcommand (add, list, done, reset)
2. The task ID or criterion ID involved
3. The criterion text, source, type, and spec (for add)

Then run the appropriate command from the examples above.
