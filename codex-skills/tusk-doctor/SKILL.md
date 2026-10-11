---
name: tusk-doctor
description: Diagnose Tusk installation, skill discovery, hooks, version skew and task workspace routing.
---

# tusk-doctor

Read [runtime guidance](references/runtime.md). Diagnose before changing anything.

- Installation/version: locate the actual wrapper, `tusk version` and `tusk path`.
- Skills: `tusk skills check --format json` and `tusk skill-drift` for missing files,
  custom conflicts, broken references, duplicate names and unsupported CLI commands.
- Hooks: `tusk hooks check` and `tusk hooks check --codex`; installed registration
  cannot prove Codex trust. Review activation in the host's hooks UI.
- Workspace: `tusk branch-parse`, `tusk task-worktree list` and the selected task's
  `task-get` state. Do not infer ownership from the newest project session.
- Configuration: `tusk validate`; report its actual failures separately from advisories.

Repair only within the user's request: skills install, hooks install, configuration
repair or an authorized upgrade. Preserve custom files; explain conflicts needing a
choice with exact paths. Never clear task data or grant hook trust. If the CLI predates
these checks, report the unavailable capability and use supported read-only diagnostics.
