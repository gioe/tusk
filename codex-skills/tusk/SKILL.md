---
name: tusk
description: Execute a Tusk task through implementation, verification, review and finalization.
---

# tusk

Use the supplied task ID; otherwise select ready backlog work with `tusk task-select`.
Read [runtime guidance](references/runtime.md) for wrapper resolution and attribution.

1. Run `tusk task-start <id> --skill tusk`; retain its task, criteria, session and run IDs.
   Resume from existing progress rather than restarting completed work.
2. Create or reuse `tusk task-worktree create <id> <slug>` and work there. Read
   `task-brief` when additional context is needed; load only task-relevant files.
3. Implement against the criteria. Use targeted checks during editing and the configured
   gate before commit. Mark criteria as their actual evidence becomes available.
4. Commit with `tusk commit <id> <message> <files...> --criteria <criterion-id>`.
   Apply repository release/version rules once per change.
5. Review the task diff through `tusk review begin <id>`, fix blocking findings and
   record the truthful verdict. Use independent review when required by project policy.
6. Finalize with `tusk merge <id> --session <session-id>` when authorized by the task
   workflow. Preserve configured PR/review gates. Finish the returned skill run and
   report what shipped, checks and material limitations.

For interruption, scope changes or failed commands, read only the relevant item in
[recovery](references/recovery.md). Save next steps if work cannot finish. For creating
new work, use create-task if available or the CLI's structured task-import interface.
