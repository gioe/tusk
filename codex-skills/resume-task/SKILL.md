---
name: resume-task
description: Recover an interrupted Tusk task from its workspace, criteria and saved progress.
---

# resume-task

Read [runtime guidance](references/runtime.md). Use the supplied ID or `tusk branch-parse`
from the current branch. Read `task-get`, `task-worktree list` and latest next steps.
Confirm session ownership before attaching; do not steal another active session.

Resume the recorded task workspace and unfinished criteria. Compare saved evidence
against the current revision, keeping valid completed work. Load only linked context
needed for the next action. Continue through the normal task commit/review/merge gates;
use the tusk skill if available. Save a checkpoint if recovery remains blocked.
