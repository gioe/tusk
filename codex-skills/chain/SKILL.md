---
name: chain
description: Execute a requested dependency chain of Tusk tasks in dependency order.
---

# chain

Read [runtime guidance](references/runtime.md). Inspect the supplied head tasks and
dependency graph using `tusk chain --help` and `tusk deps`. Work ready tasks through
normal task/worktree/review gates. Do not force unresolved dependencies. Parallel agent
execution is optional and requires available authorized delegation and isolated workspaces.
After each merge, refresh readiness. Report blocked tasks and preserve next steps.
