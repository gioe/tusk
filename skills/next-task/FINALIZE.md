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
tusk task-update <id> --github-pr "<pr_url>"
```

## Step 13: Review dispatch — mode-aware pre-merge review

Check the review mode from config:

```bash
tusk config review
```

This returns the `review` object (or `null`/empty if the key is missing). Extract `review.mode`.

**Decision tree:**

### mode = disabled (or review key missing from config)

Skip AI review entirely. Proceed directly to Step 14.

### mode = ai_only

Run `/review-pr` by following the instructions in the review-pr skill. Pass the current task ID:

```
Follow the instructions in <base_directory>/../review-pr/SKILL.md for task <id>
```

The `/review-pr` skill handles:
- Spawning parallel AI reviewer agents
- Fixing `must_fix` findings
- Handling `suggest` and `defer` findings
- Printing a final verdict (APPROVED / CHANGES REMAINING)

After `/review-pr` completes with verdict **APPROVED**, proceed directly to Step 14. If verdict is **CHANGES REMAINING**, surface the unresolved items to the user and stop — do not merge.

### mode = ai_then_human

First, run `/review-pr` exactly as in `ai_only` above. After the AI review is complete and verdict is APPROVED, then wait for human GitHub review:

Poll for human review approval:

```bash
gh pr view <pr_number> --json reviewDecision,reviews
```

Repeat every 60 seconds until `reviewDecision` is `"APPROVED"`. While waiting, print:

> Waiting for human GitHub review approval... (checking every 60s)
> Current status: <reviewDecision>

Once human review shows `APPROVED`, proceed to Step 14.

If a human reviewer requests changes, address the feedback:

**Category A — Address Immediately (must fix in this PR):**
- Security concerns, bugs, breaking changes
- Missing tests for code introduced/modified in this PR
- Performance issues, type errors, missing error handling

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

After addressing feedback, re-run `/review-pr` and re-poll for human approval until `reviewDecision` is `"APPROVED"`.

## Step 14: PR approved — finalize, merge, and retro

Execute steps 14-16 as a single uninterrupted sequence — do NOT pause for user confirmation between them.

Finalize the task in a single call — this sets `github_pr`, closes the session (capturing diff stats before the branch is deleted), merges the PR with `--squash --delete-branch`, and marks the task Done:

```bash
tusk finalize <id> --session $SESSION_ID --pr-url "$PR_URL" --pr-number $PR_NUMBER
```

This returns JSON including an `unblocked_tasks` array (from `tusk task-done`). If there are newly unblocked tasks, note them in the retro.

## Step 16: Run retrospective

Mandatory — run immediately without asking. Invoke `/retro` to review the session, surface process improvements, and create any follow-up tasks. Do NOT ask "shall I run retro?" — just run it.
