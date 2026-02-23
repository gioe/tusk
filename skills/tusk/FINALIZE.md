# Tusk: Finalize, PR & Merge

Steps 11-14 of the tusk workflow. Read this after implementation is complete and all acceptance criteria are verified.

## Step 11: Push the branch and create a PR

```bash
git push -u origin feature/TASK-<id>-description
gh pr create --base "$DEFAULT_BRANCH" --title "[TASK-<id>] Brief task description" --body "..."
```

Capture the PR URL from the output.

## Step 12: Review dispatch — mode-aware pre-merge review

Check the review mode from config:

```bash
tusk config review
```

This returns the `review` object (or `null`/empty if the key is missing). Extract `review.mode`.

**Decision tree:**

### mode = disabled (or review key missing from config)

Skip AI review entirely. Proceed directly to Step 13.

### mode = ai_only

Run `/review-commits` by following the instructions in the review-commits skill. Pass the current task ID:

```
Follow the instructions in <base_directory>/../review-commits/SKILL.md for task <id>
```

The `/review-commits` skill handles:
- Spawning parallel AI reviewer agents
- Fixing `must_fix` findings
- Handling `suggest` and `defer` findings
- Printing a final verdict (APPROVED / CHANGES REMAINING)

After `/review-commits` completes with verdict **APPROVED**, proceed directly to Step 13. If verdict is **CHANGES REMAINING**, surface the unresolved items to the user and stop — do not merge.

## Step 13: PR approved — finalize, merge, and retro

Execute steps 13-14 as a single uninterrupted sequence — do NOT pause for user confirmation between them.

Finalize the task in three steps:

```bash
# 1. Close the session (captures diff stats before the branch is deleted)
tusk session-close $SESSION_ID

# 2. Merge the PR
gh pr merge $PR_NUMBER --squash --delete-branch

# 3. Mark task Done
tusk task-done <id> --reason completed --force
```

`tusk task-done` returns JSON including an `unblocked_tasks` array. If there are newly unblocked tasks, note them in the retro.

## Step 14: Run retrospective

Mandatory — run immediately without asking. Invoke `/retro` to review the session, surface process improvements, and create any follow-up tasks. Do NOT ask "shall I run retro?" — just run it.
