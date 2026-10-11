---
name: tusk-init
description: Initialize Tusk project configuration and an optional initial backlog after installation.
---

# tusk-init

Read [runtime guidance](references/runtime.md). Inspect existing configuration and
project scaffolding before treating the project as new. Use `tusk init-wizard --help`
and its current CLI contract to collect intent and generate configuration. Preserve
existing database/configuration; never use init --force as a routine repair.

Infer routine metadata from the repository and ask only about missing project intent
or consequential preferences. Seed approved tasks with task-import after deduplication.
Validate configuration, explain installed skill/hook status, and distinguish installed
hooks from user-granted trust. See tusk-doctor when diagnosing an existing install.
