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


