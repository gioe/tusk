---
name: tusk-update
description: Update an existing Tusk project configuration while preserving its database and local conventions.
---

# tusk-update

Read [runtime guidance](references/runtime.md). Read current config and the requested
change. Use supported configuration commands where available; preserve unrelated keys
and custom values. After changing validation enums, run `tusk regen-triggers`, then
`tusk validate`. Do not reinitialize the database to update configuration.

Reconcile project-type skills when that type changes. Native optional groups are managed
with `tusk skills install --groups core,reporting` (the list replaces the selection).
Use `tusk skills check` to inspect conflicts. Upgrade the distribution only when requested
or needed for the requested capability; do not overwrite customized skills to repair drift.
