---
name: create-task
description: Turn approved requirements into scoped Tusk tasks with criteria, dependencies and durable context.
---

# create-task

Read [runtime guidance](references/runtime.md). Clarify only material scope ambiguities;
existing approval to create the described work is sufficient.

Read `tusk setup` for configured metadata and open backlog; check potential duplicates.
Choose independently shippable tasks, each with a concrete description, scoped paths,
acceptance criteria and proportionate verification. Capture decisions, risks and entry
points future agents need without the chat. Avoid creating tasks for mere observations.

Prefer `tusk task-import --file <json> --dry-run`, then import the validated batch.
Use local dependency keys for real ordering constraints, link an existing objective
when relevant, and preserve context with `tusk context add`. Check returned IDs and
partial failures before retrying. Report created/skipped tasks; task creation alone
does not authorize executing them or sending external messages.
