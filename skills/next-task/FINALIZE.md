# Next Task: Finalize, PR & Merge

Steps 12-17 of the next-task workflow. Read this after implementation is complete and all acceptance criteria are verified.

## Step 12: Push the branch and create a PR

```bash
git push -u origin feature/TASK-<id>-description
gh pr create --base "$DEFAULT_BRANCH" --title "[TASK-<id>] Brief task description" --body "..."
```

Capture the PR URL from the output.

## Step 13: Update the task with the PR URL

```bash
tusk "UPDATE tasks SET github_pr = $(tusk sql-quote "<pr_url>"), updated_at = datetime('now') WHERE id = <id>"
```

## Step 14: Review loop — iterate until approved

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
4. Log a progress checkpoint (step 8) after each review-fix commit

**Category B — Defer to backlog (cosmetic only):**
- Pure style preferences not affecting correctness
- Suggestions about pre-existing code NOT touched by this PR
- Aspirational ideas about unrelated modules

For each Category B comment:
1. **Check for duplicates first** using `/check-dupes`:
   ```bash
   tusk dupes check "[Deferred] <brief description>" --domain <domain>
   ```
2. Create a deferred task (with 60-day expiry):
   ```bash
   tusk "INSERT INTO tasks (summary, description, status, priority, domain, created_at, updated_at, expires_at)
     VALUES ($(tusk sql-quote "[Deferred] <brief description>"), $(tusk sql-quote "Deferred from PR #<pr_number> review for TASK-<id>.

Original comment: <comment text>

Reason deferred: <why this can wait>"), 'To Do', 'Low', '<domain>', datetime('now'), datetime('now'), datetime('now', '+60 days'))"
   ```

## Step 15: PR approved — finalize, merge, and retro

Execute steps 15-17 as a single uninterrupted sequence — do NOT pause for user confirmation between them.

Close the session **before** merging (captures diff stats from the feature branch, which is deleted after merge):
```bash
tusk session-close $SESSION_ID
```

Merge and delete the feature branch:
```bash
gh pr merge $PR_NUMBER --squash --delete-branch
```

Update task status:
```bash
tusk "UPDATE tasks SET status = 'Done', closed_reason = 'completed', updated_at = datetime('now') WHERE id = <id>"
```

## Step 16: Check for newly unblocked tasks

```bash
tusk -header -column "
SELECT t.id, t.summary, t.priority
FROM tasks t
JOIN task_dependencies d ON t.id = d.task_id
WHERE d.depends_on_id = <id> AND t.status = 'To Do'
"
```

## Step 17: Run retrospective

Mandatory — run immediately without asking. Invoke `/retro` to review the session, surface process improvements, and create any follow-up tasks. Do NOT ask "shall I run retro?" — just run it.
