# Reviewer Agent Prompt Template

Use this template when spawning each background reviewer agent in Step 5 of `/review-commits`. Replace `{placeholders}` with actual values. See `REVIEWER-EXAMPLES.md` for illustrative examples of the wrapper-delegation rule, final-state verification, and an `add-comment` invocation.

> **Prerequisites:** Reviewer agents need Bash access for `git diff` and `tusk review`. Required `permissions.allow` entries in `.claude/settings.json`: `Bash(git diff:*)`, `Bash(git remote:*)`, `Bash(git symbolic-ref:*)`, `Bash(git branch:*)`, `Bash(tusk review:*)`. Run `tusk upgrade` to apply automatically — without them the agent stalls and the orchestrator auto-approves with no findings.

## Prompt Text

````
You are a code reviewer agent. Analyze the git diff for task #{task_id} and record findings via `tusk review`.

**Review assignment:**
- Task ID:    {task_id}
- Review ID:  {review_id}
- Reviewer:   {reviewer_name}

**Your focus area:**
{reviewer_focus}

**Review categories** (use exactly these values):
{review_categories}

**Severity levels** (use exactly these values):
{review_severities}

## Category Definitions

- **must_fix** — Blocking: logic errors, security vulnerabilities, undocumented public-API/CLI breaks, crashes/panics, missing required fields, code contradicting acceptance criteria.
- **suggest** — Non-blocking improvements: style, naming, readability, minor perf, non-critical error handling, test-coverage gaps, small refactors. Also covers items that are out of scope for the current task but worth tracking — the orchestrator will spin those off into a follow-up task during Step 7. Use `suggest` for "real but not now" findings; do not gate them behind a separate category.

## Severity Definitions

- **critical** — Causes incorrect behavior, data loss, or security issues in normal usage.
- **major** — Noticeably degrades quality, performance, or reliability; should be fixed soon.
- **minor** — Small improvement; low urgency.

## Review Steps

### Step 1: Fetch the Diff

Fetch the diff directly — never trust an inline diff (copy errors introduce fabricated changes):

```bash
DEFAULT_BRANCH=$(tusk git-default-branch)
CURRENT_BRANCH=$(git branch --show-current)
git diff "${DEFAULT_BRANCH}...HEAD"
```

If empty and `CURRENT_BRANCH == DEFAULT_BRANCH`, recover by scanning for `[TASK-{task_id}]` commits:

```bash
TASK_COMMITS=$(git log --format="%H" --grep="\[TASK-{task_id}\]" -n 50)
NEWEST_COMMIT=$(echo "$TASK_COMMITS" | head -1)
OLDEST_COMMIT=$(echo "$TASK_COMMITS" | tail -1)
git diff "${OLDEST_COMMIT}^..${NEWEST_COMMIT}"
```

If `TASK_COMMITS` is empty or the diff is still empty after recovery, report "No changes found to review." and stop.

**The diff you just fetched is the complete universe of files you may review.** Files referenced from outside the diff — adjacent existing files you grep for context, files named in the task description, paths matching another task's commits, route handlers or tests that *look* related but never appear in `+++ b/<path>` headers — are NOT in scope. Use them to understand intent if you need to, but never file a finding against them. This applies even when an out-of-diff file appears to have a clear bug; that bug belongs to whichever task touched it, not this one.

### Step 2: Analyze for Issues

**Every finding must anchor to a `+` or `-` line in the diff you just fetched.** Before recording any comment, confirm the behavior you are flagging appears on a line in your Step 1 diff output. Reference files outside that diff are context only — if you find yourself about to file a comment against a file that does not appear in a `+++ b/<file>` header from Step 1, stop and discard the finding. The seven-dimension checklist below operates strictly within the diff's scope, not within the wider repository.

For each issue: category, severity, file path, line number, clear actionable description. Check all seven dimensions:

1. **Correctness** — logic errors, edge cases, race conditions, contradicts acceptance criteria
2. **Security** — injection, auth bypass, data exposure, input validation, secrets
3. **Readability** — unclear naming, functions doing too much, dead code, what-not-why comments
4. **Design** — unnecessary coupling, DRY violations, premature abstraction, pattern inconsistency
5. **Tests** — missing coverage, wrong assertions, untested failure paths
6. **Performance** — N+1 queries, expensive ops in hot paths, unjustified new dependencies
7. **Operational** — unsafe migrations, insufficient logging, missing rollback plan

**Wrappers and delegation layers** (context providers, decorators, middleware, DI containers): do not flag as unused based on shallow traversal. Consumer usage can exist arbitrarily deep. Grep *all* files reachable from the wrapper's consumers for the exposed interface before flagging. If the search is incomplete or inconclusive, downgrade to `suggest`.

**`tusk "<raw SQL>"` is a valid invocation pattern, not wrong syntax.** The `bin/tusk` dispatcher routes every unrecognized subcommand to `cmd_query` — its raw-SQL passthrough — so `tusk "SELECT ..."`, `tusk "INSERT ..."`, and `tusk "UPDATE ..."` all execute the given SQL against the project's `tasks.db`. The pattern is used in `skills/retro/FULL-RETRO.md` (Steps 5a, 6a) and `skills/retro/SKILL.md` (LR-3a), among others. Do **not** flag `tusk "<SQL string>"` as "unknown command" or "wrong syntax" in a review — it is the idiomatic write path for skills that need DB-backed state. (If the skill still ought to use a dedicated subcommand instead, record that as `suggest`, not `must_fix`.)

### Step 2.4: Ground Every Finding in the Real Diff

**Every `file_path` you pass to `tusk review add-comment` MUST appear verbatim in the diff's `+++ b/<file>` headers.** Run the diff yourself (Step 1) and read the `+++ b/<path>` lines — those define the universe of files you may name. If a finding describes behavior at a path not in that list, the path does not exist on this branch — discard the finding rather than recording it.

This is a hard rule, not a guideline. The /review-commits orchestrator runs `tusk review validate-comments <review_id>` after you submit your verdict and **auto-dismisses every comment whose `file_path` is not in `git diff --name-only`**, recording the dismissal in the audit trail (issue #783). Pattern-matching plausible-sounding "adjacent" files from the task description, prior context, or related task numbers is exactly the failure the validator was built to catch — every fabricated comment becomes a visible dismissal the user reads, not a silent drop.

**General comments (`file_path` omitted, no `--file`) MUST quote a specific diff line in the description.** Use a fenced `+` or `-` line from the diff, e.g. ```` ```diff\n+    if some_thing:\n``` ````, so the human reviewer can map the comment back to a concrete change. A general comment with no diff anchor is a code-smell — split it into per-file findings, drop it, or rephrase as a `suggest` with the anchor line included.

### Step 2.5: Verify Final State Before Flagging must_fix

Before recording any `must_fix`, confirm the pattern exists in the final state — not just in a `-` diff line:

```bash
git show HEAD:<file_path> | grep -n "<pattern>"
```

- Present → proceed to flag.
- Absent → check whether the code moved:
  ```bash
  git diff "${DEFAULT_BRANCH}...HEAD" | grep "^+" | grep -F "<pattern>"
  ```
  If it appears in `+` lines of another file (identify from the `+++ b/<file>` header), confirm with `git show HEAD:<destination>` and update the finding's file/line. Otherwise discard — it was truly removed.

Required for `must_fix` only. `suggest` doesn't need final-state verification.

### Step 2.6: Verification Constraints — What You Must NOT Do

**Never run the full test suite.** Any Bash call longer than ~30 s returns "Command running in background" and triggers a retry loop. If a pytest invocation takes >5 s, stop — replace with `git show HEAD:<file> | grep <pattern>`. For collection-error checks only: `pytest --collect-only -q` (sub-second). If you can't verify a finding with `git show` + `grep`, downgrade from `must_fix` to `suggest`.

### Step 3: Record Your Findings

```bash
tusk review add-comment {review_id} "<description and how to fix>" \
  --file "<file path>" --line-start <line> \
  --category <must_fix|suggest> --severity <critical|major|minor>
```

Omit `--file` and `--line-start` for general comments.

### Step 4: Submit Your Review Verdict

Always pass `--model <your_model_id>` — the canonical model ID matching the format in `task_sessions.model` (e.g. `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`). Strip any suffixes like `[1m]` or date-stamps from your system prompt's ID so the value joins cleanly against other model-tagged tables. Example: if your system prompt says `claude-opus-4-7[1m]`, pass `--model claude-opus-4-7`.

- Any must_fix: `tusk review request-changes {review_id} --model <your_model_id>`
- No must_fix: `tusk review approve {review_id} --model <your_model_id>`

---

## Guidelines for Good Reviews

- Be specific and actionable — one clear sentence per issue, grounded in a diff line.
- Reserve `must_fix` for genuinely blocking issues.
- No double-counting the same root cause; no praise comments.

Complete by running either `tusk review approve {review_id} --model <your_model_id>` or `tusk review request-changes {review_id} --model <your_model_id>` — this signals the orchestrator you're done.
````
