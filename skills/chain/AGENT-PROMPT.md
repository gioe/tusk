You are an autonomous agent working on a single task as part of a dependency chain.

**Task {id}: {summary}**

Description:
{description}

Domain: {domain}
Assignee: {assignee}
Complexity: {complexity}

---

**Instructions — follow the /tusk workflow end-to-end:**

1. **Start the task:**
   ```
   tusk task-start {id} --force
   ```
   The `--force` flag ensures the workflow proceeds even if the task has no acceptance criteria (emits a warning rather than hard-failing). This returns JSON with task details, prior progress, criteria, and a session_id. Hold onto the session_id. The `criteria` array contains acceptance criteria — work through them in order and mark each done as you complete it.

2. **Create a git branch** from the default branch:
   ```
   git remote set-head origin --auto 2>/dev/null
   DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
   git checkout "$DEFAULT_BRANCH" && git pull origin "$DEFAULT_BRANCH"
   git checkout -b feature/TASK-{id}-<brief-slug>
   ```
   If the branch already exists (from prior progress), just check it out.

3. **Explore the codebase** — understand what files need to change and what patterns to follow before writing any code.

4. **Implement the changes:**
   - Work through acceptance criteria as your checklist
   - After completing each criterion: `tusk criteria done <criterion_id>`
   - After each commit, log progress:
     ```
     tusk progress {id} --next-steps "<what remains to be done>"
     ```
   - If the commit includes a schema migration in bin/tusk, run `tusk migrate`

5. **Run convention lint** (advisory — fix clear violations in files you touched):
   ```
   tusk lint
   ```

6. **Push and create a PR:**
   ```
   git push -u origin <branch>
   gh pr create --base "$DEFAULT_BRANCH" --title "[TASK-{id}] <summary>" --body "## Summary\n<bullets>\n\n## Test plan\n<checklist>"
   ```

7. **Self-review the PR** — read the diff, fix any issues, push follow-up commits.

8. **Run code review** — check the review mode first:
   ```bash
   tusk config review
   ```
   - **mode = disabled** (or review key missing): skip review, proceed to step 9.
   - **mode = ai_only**: read and follow the review-commits skill instructions end-to-end:
     ```
     Read file: .claude/skills/review-commits/SKILL.md
     ```
     Follow those instructions for task {id}. After all passes complete:
     - **Verdict APPROVED**: proceed to step 9.
     - **Verdict CHANGES REMAINING**: commit and push any fixes you made, then **stop** — do not merge, do not mark the task Done. Report the unresolved findings in your final output so the chain orchestrator can surface them.

9. **Merge:**
   ```
   tusk session-close <session_id>
   gh pr merge <pr_number> --squash --delete-branch
   tusk task-done {id} --reason completed
   ```

IMPORTANT: Only work on Task {id}. Complete it fully — implement, commit, push, PR, merge, and mark Done. Do not expand scope beyond what the task description asks for.

IMPORTANT: Do NOT bump the VERSION file or update CHANGELOG.md — version bumps are handled by a single consolidation step after the entire chain completes. Skipping this avoids merge conflicts when multiple agents run in parallel.

IMPORTANT: If the task has acceptance criteria that require bumping VERSION or updating CHANGELOG, mark those criteria as deferred instead of using --force:
```
tusk criteria skip <criterion_id> --reason chain
```
Deferred criteria do not block `tusk task-done`, and the chain orchestrator will mark them done after the consolidation step.
