---
name: review-commits
description: Review task commits, record actionable findings and verify fixes using Tusk review records.
---

# review-commits

Read [runtime guidance](references/runtime.md). Use `tusk review begin <task-id>` to
obtain the review ID and exact commit range. Read that diff and relevant callers/tests.
Evaluate behavior, regressions, scope and acceptance evidence. Follow the repository's
reviewer independence requirements; do not claim an independent review of your own work.

Record concrete findings with `review add-comment` using configured categories and
severities. Distinguish blocking defects from suggestions. Fix findings when execution
is authorized, verify affected behavior, and resolve only with evidence. Preserve useful
nonblocking follow-ups as context or approved tasks. Approve only when no blocking
findings remain. A review request alone does not authorize merge, publication or messaging.
