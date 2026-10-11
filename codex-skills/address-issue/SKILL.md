---
name: address-issue
description: Turn a supplied GitHub issue into a Tusk task and implement it when requested.
---

# address-issue

Read [runtime guidance](references/runtime.md). Fetch the supplied issue using the
configured GitHub repository; inspect existing task links before creating duplicates.
Preserve the issue's requirements, entry points and source URL in task context. Use
create-task or task-import for approved task creation, then the standard Tusk workflow
when implementation is requested. External comments, issue closure and publication
require user authorization; do not infer it from a read-only issue investigation.
