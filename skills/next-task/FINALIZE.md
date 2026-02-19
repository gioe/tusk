# Next Task: Finalize, PR & Merge

Steps 11-16 of the next-task workflow. Read this after implementation is complete and all acceptance criteria are verified.

## Step 11: Push the branch and create a PR

```bash
git push -u origin feature/TASK-<id>-description
gh pr create --base "$DEFAULT_BRANCH" --title "[TASK-<id>] Brief task description" --body "..."
```

Capture the PR URL from the output.

## Step 12: Update the task with the PR URL

```bash
tusk "UPDATE tasks SET github_pr = $(tusk sql-quote "<pr_url>"), updated_at = datetime('now') WHERE id = <id>"
```

## Step 13: Review loop — iterate until approved

Poll for review → analyze feedback → address or defer → push fixes → repeat until approved.

**Category A — Address Immediately (must fix in this PR):**
- Security concerns, bugs, breaking changes
- Missing tests for code introduced/modified in this PR
- Performance issues, type errors, missing error handling

The bar is: if the reviewer comments on code this PR touches, fix it now.

For each Category A comment:
1. Read the relevant file(s)
2. Make the code fix
3. Commit: `[TASK-<id>] Address PR review: <brief description>`
4. Log a progress checkpoint (step 7) after each review-fix commit

**Category B — Defer to backlog (cosmetic only):**
- Pure style preferences not affecting correctness
- Suggestions about pre-existing code NOT touched by this PR
- Aspirational ideas about unrelated modules

For each Category B comment, create a deferred task (includes built-in duplicate check and 60-day expiry):
   ```bash
   tusk task-insert "<brief description>" "Deferred from PR #<pr_number> review for TASK-<id>. Original comment: <comment text>. Reason deferred: <why this can wait>" \
     --priority "Low" --domain "<domain>" --deferred
   ```
   Exit code 1 means a duplicate was found — skip silently.

## Step 14: PR approved — finalize, merge, and retro

Execute steps 14-16 as a single uninterrupted sequence — do NOT pause for user confirmation between them.

Close the session **before** merging (captures diff stats from the feature branch, which is deleted after merge):
```bash
tusk session-close $SESSION_ID
```

Merge and delete the feature branch:
```bash
gh pr merge $PR_NUMBER --squash --delete-branch
```

Mark the task Done and check for newly unblocked tasks:
```bash
tusk task-done <id> --reason completed
```

This closes any remaining open sessions, sets status to Done with the closed_reason, and returns JSON including an `unblocked_tasks` array. If there are newly unblocked tasks, note them in the retro.

## Step 16: Run retrospective

Mandatory — run immediately without asking. Invoke `/retro` to review the session, surface process improvements, and create any follow-up tasks. Do NOT ask "shall I run retro?" — just run it.
