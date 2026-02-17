---
name: lint-conventions
description: Check codebase for violations of tusk project conventions
allowed-tools: Bash
---

# Lint Conventions

Checks the tusk codebase against Key Conventions from CLAUDE.md. Rules are defined in `bin/tusk-lint.py` and executed via the `tusk lint` CLI command. Run this before releasing or as a pre-PR sanity check.

## How It Works

Run `tusk lint` and present the results:

```bash
tusk lint
```

The command exits with status 0 if no violations are found, or status 1 if there are violations.

## Interpreting Results

The output groups findings by rule. Each rule shows either `PASS` or `WARN` with specific `file:line` locations:

- **PASS** — No violations for that rule.
- **WARN** — One or more violations detected. Review each listed file and line.

If violations are found, fix them before proceeding with your PR or release.

## Adding New Rules

New rules are added to `bin/tusk-lint.py`, not to this skill. See the existing rule functions in that file for the pattern to follow.

## Integration Points

This skill can be referenced by:
- **`/next-task`** — As an optional pre-PR check
- **`/retro`** — To audit convention compliance after a session
