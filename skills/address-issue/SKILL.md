---
name: address-issue
description: Fetch a GitHub issue, create a tusk task from it, and work through it with /tusk
allowed-tools: Bash, Read, Edit, Write, Grep, Glob
---

# Address Issue Skill

Fetches a GitHub issue, converts it into a tusk task, and immediately begins working on it using the full `/tusk` workflow.

## Step 1: Parse the Issue Reference

The user invokes this skill with an optional issue number or full URL. Examples:
- `/address-issue 314`
- `/address-issue https://github.com/gioe/tusk/issues/314`
- `/address-issue` *(no argument — defaults to newest open issue)*

Extract the issue number:
- If a full URL is given, parse the number from the path.
- If only a number is given, use it directly.
- If **no argument was provided**, fetch the newest open issue automatically:
  ```bash
  gh issue list --repo gioe/tusk --state open --limit 1 --json number,title
  ```
  If this returns an empty list, report:
  > No open issues found in gioe/tusk.

  Then stop. Otherwise, use the returned `number` and display:
  > No issue specified — defaulting to newest open issue: #<number> "<title>"

## Step 2: Fetch the Issue

Use `gh` to fetch the issue. Detect the repo from the argument:
- If a full URL was given, extract `owner/repo` from it.
- If only a number was given, default to `gioe/tusk`.

```bash
gh issue view <number> --repo <owner/repo> --json number,title,body,labels,comments,state
```

If the issue is already closed (`state: "CLOSED"`), warn the user:

> Issue #<N> is already closed. Do you still want to create a task for it?

Wait for confirmation before proceeding.

## Step 3: Fetch Config and Backlog

```bash
tusk setup
```

Store the `config` (domains, task_types, agents, priorities, complexity) and `backlog` (for duplicate detection).

## Step 4: Analyze the Issue and Determine Task Fields

Using the issue `title`, `body`, and `labels`, determine:

| Field | How to Determine |
|-------|-----------------|
| **summary** | Derive from the issue title — keep it imperative and under ~100 chars. Prefix with "Fix:" for bugs, otherwise use the title as-is or rephrase as an action. |
| **description** | Include the full issue body as context, plus the issue URL as a reference link. Format: `GitHub Issue #<N>: <url>\n\n<body>` |
| **priority** | Infer from labels: `priority: high` / `critical` / `urgent` → `High`/`Highest`; `priority: low` → `Low`; labels like `bug` or `regression` → lean `High`; default `Medium`. |
| **domain** | Match the issue's subject area to a configured domain. Leave NULL if no match. |
| **task_type** | `bug` for issues labeled `bug` or `defect`; `feature` for `enhancement`/`feature request`; `docs` for `documentation`; otherwise `feature`. |
| **assignee** | Match to a configured agent if the domain/labels clearly indicate one. Leave NULL if unsure. |
| **complexity** | Estimate from the issue body length and scope. Short reproduction steps with a clear fix → `S`; broad feature request → `M`; major architectural change → `L`. |

Generate **3–7 acceptance criteria** from the issue body — concrete, testable conditions. For bug issues, always include a criterion that the failure case is resolved and a regression test criterion.

## Step 4.1: Extract Failing Test Criterion

Scan the issue body for a `## Failing Test` section. If present:

1. Extract the fenced code block immediately following that heading. Support both triple-backtick and single-backtick fences:
   - Triple-backtick: content between ` ``` ` (optionally with a language tag on the opening fence) and the next ` ``` `
   - Single-backtick: content between a single `` ` `` open and close on the same or adjacent lines
   - Take the **first** fenced block found in the section; trim leading/trailing whitespace from the extracted content.

2. **Validate the extracted spec** — run it immediately and capture the exit code and stderr:
   ```bash
   bash -c '<test_spec>' 2>/tmp/tusk_test_spec_stderr.txt
   SPEC_EXIT=$?
   SPEC_STDERR=$(cat /tmp/tusk_test_spec_stderr.txt)
   ```

   Interpret the result:

   - **Exit nonzero and no command error** — the spec fails as expected on the current codebase. Store as `test_spec` and proceed normally.

     > **Note:** Before storing, verify the spec actually calls into the project under test (e.g., runs a CLI command, imports a project module, or references a real project file). A spec that embeds its own logic — defining its own functions inline rather than calling the real implementation — may exit nonzero (demonstrating the bug) yet pass trivially once its inline logic is corrected, without ever exercising the actual fix. If the spec appears self-contained, surface this during Step 7 and ask the implementer to validate the criterion manually before marking it done.

   - **Exit 0** — the spec passes *before* any fix. This indicates a self-contained demonstration or already-resolved issue. Warn the implementer:
     > **Warning:** The `## Failing Test` spec exits 0 on the current codebase — it may not be a real regression test. Discard it (treat as `test_spec=null`, `test_present="no"`) or keep it with this warning attached?

     Wait for confirmation:
     - **Discard** → set `test_spec = null`; score `test_present` as `"no"` (contributes negatively for `bug`/`defect` types; N/A otherwise).
     - **Keep** → store as `test_spec` with a `(warning: passed before fix)` note appended; proceed normally.

   - **Command error** (exit code 126/127, or stderr contains "command not found", "not found", or "syntax error") — the spec is not a runnable shell command. Treat as `test_spec = null` and score `test_present` as `"no"`. Inform the implementer:
     > The `## Failing Test` spec produced a command error (`<first line of SPEC_STDERR>`). Treating as no failing test.

3. **If no `## Failing Test` section is found**, set `test_spec = null`. No test criterion will be added in Step 6. For `bug` and `defect` task types, the absence will bias the Step 4.7 verdict toward Defer via the `test_present` factor. For all other task types (`docs`, `feature`, `refactor`, etc.), the `test_present` factor is N/A — missing a failing test does not bias the verdict.

> **Note:** The test criterion is marked done by running `tusk criteria done <cid>`. Because `criterion_type=test`, tusk automatically executes `verification_spec` as a shell command and blocks closure if it exits nonzero. No manual check is required — the spec must pass on its own.

## Step 4.5: Optional Codebase Investigation

**Skip this step entirely if complexity is XS or S.** Only run for M, L, or XL complexity issues.

After completing Step 4, offer the user a chance to investigate the codebase before presenting the task proposal:

> Before presenting the proposal, should I investigate the codebase for context? This helps surface relevant files, existing patterns, and edge cases that could sharpen the acceptance criteria. (**yes** / **no**, default: no)

Wait for the user's response. Treat any response other than an explicit **yes** as **no** — skip directly to Step 5 with no changes.

If the user says **yes**:

1. **Run a read-only investigation.** Use only these tools: `Read`, `Grep`, `Glob`, and `Bash` (restricted to read-only operations: `tusk` CLI queries, `ls`, directory inspection — no write commands, no `git commit`, no file edits). Limit the investigation to ~10 tool calls; stop and summarize what you found even if the picture is incomplete. Explore:
   - Files and functions directly related to the issue's subject area (search by keyword, class name, or config key mentioned in the issue body)
   - Existing tests for the affected code paths
   - Any conventions or patterns already established for similar features
   - Whether a partial implementation already exists

   Example queries:
   - Use the `Grep` tool to find files mentioning a keyword from the issue
   - Use the `Glob` tool to locate relevant test files
   - Query the tusk backlog for related tasks:
     ```bash
     tusk task-list --format json | python3 -c "import sys,json; tasks=json.load(sys.stdin); [print(t['id'], t['summary']) for t in tasks if '<keyword>' in t['summary'].lower()]"
     ```

2. **Summarize the findings** in a short block before proceeding:

   > **Codebase Investigation Findings:**
   > - <finding 1>
   > - <finding 2>
   > ...

3. **Refine the task fields** from Step 4 based on what you found:
   - Adjust `description` to reference specific files or functions that will need to change.
   - Add, remove, or sharpen acceptance criteria to reflect actual code structure (e.g., replace a vague criterion with one that names the specific function or test file).
   - Adjust `complexity` up or down if the codebase evidence warrants it.
   - Do **not** change the `summary`, `priority`, or `domain` unless the investigation reveals a fundamental misclassification.

Proceed to Step 5 with the updated task fields.

## Step 4.6: Reproducibility Check (bug-type only)

**Run this step only when `task_type = bug`.** Skip for all other task types.

Before presenting the proposal, quickly scan the codebase to confirm the bug is still present. Use at most 3 tool calls (Grep, Read, or Bash read-only). If you find clear evidence the bug is already fixed (e.g., the code path described in the issue no longer exists or has been corrected), surface this before proceeding:

> **Reproducibility note:** The issue may already be fixed — [brief explanation]. Do you still want to create a task?

Wait for user confirmation before proceeding to Step 5. If the bug is confirmed still present, or if you cannot determine either way within 3 calls, proceed without comment.

## Step 4.7: Model Recommendation (Config-Driven Scoring)

Read `issue_scoring` from the config fetched in Step 3:

```
scoring    = config["issue_scoring"]
factors    = scoring["factors"]
thresholds = scoring["thresholds"]
```

Evaluate each factor and look up its score contribution from `factors`:

| Factor key | Condition to evaluate | Value key |
|---|---|---|
| `test_present` | Was a `## Failing Test` section found in Step 4.1? **Only evaluate for `bug` and `defect` task types.** For all other task types (`docs`, `feature`, `refactor`, etc.), treat as N/A: contribution = 0 regardless of presence or absence. | `"yes"` / `"no"` |
| `pillar_aligned` | Does the issue align with the project pillars (run `tusk pillars list` to fetch `[{id, name, core_claim}]`)? If the list is empty, skip (contribution = 0). | `"yes"` / `"no"` |
| `duplicate` | Is an open task already covering this issue (from Step 3 backlog)? Include the task ID in the rationale if yes. | `"yes"` / `"no"` |
| `in_scope` | Does the issue fit the project's stated purpose? | `"yes"` / `"no"` |
| `severity_high` | Does inaction risk data loss, user-facing breakage, or a security vulnerability? | `"yes"` / `"no"` |
| `issue_quality` | Is the report clear, reproducible, and actionable? | `"good"` / `"poor"` |

For each factor: `contribution = factors[factor_key][value_key]`

Compute: `total = sum of all factor contributions`

Assign verdict from thresholds:
- `total >= thresholds["address"]` → **Address**
- `total <= thresholds["decline"]` → **Decline**
- Otherwise → **Defer**

Record the verdict, per-factor contributions, total, and a 1–2 sentence rationale for display in Step 5.

## Step 5: Present Proposed Task for Review

Open with a **Model Recommendation** block (including the score breakdown from Step 4.7), then show the proposed task:

```markdown
### Model Recommendation

> **Recommendation: <Address / Defer / Decline>** — <1–2 sentence rationale from Step 4.7>
>
> **Score:** test_present: <±N>, pillar_aligned: <±N>, duplicate: <±N>, in_scope: <±N>, severity_high: <±N>, issue_quality: <±N> → **total: <N>** (Address ≥ <thresholds.address>, Decline ≤ <thresholds.decline>)

## Proposed Task from Issue #<N>

**<summary>** (<priority> · <domain> · <task_type> · <complexity>)
> <description preview — first 2 sentences>

**Acceptance Criteria:**
1. <criterion 1>
2. <criterion 2>
...
```

Then ask, **bolding the option that matches the recommendation**:

- If recommendation is **Address**:
  > Create this task? You can **confirm** (implement now), defer (add to backlog, no immediate work), edit (e.g., "change priority to High"), decline (close the issue without creating a task), or cancel.

- If recommendation is **Defer**:
  > Create this task? You can confirm (implement now), **defer** (add to backlog, no immediate work), edit (e.g., "change priority to High"), decline (close the issue without creating a task), or cancel.

- If recommendation is **Decline**:
  > Create this task? You can proceed anyway (implement now), defer (add to backlog, no immediate work), edit (e.g., "change priority to High"), **decline** (close the issue without creating a task), or cancel.

The user retains full veto power — any option may be chosen regardless of the recommendation.

Wait for explicit user approval before inserting.

### Decline Path

If the user types **decline** (optionally followed by a rationale, e.g., `decline out of scope`):

1. If no rationale was given inline, prompt:
   > Why are you declining this issue? Choose one or describe:
   > - `out of scope`
   > - `won't fix`
   > - `already handled by TASK-<id>`
   > - `duplicate of #<issue>`
   > - `other: <free text>`

   Wait for the user's response.

2. Close the GitHub issue with the rationale as a comment:
   ```bash
   gh issue close <number> --repo <owner/repo> --comment "Declined: <rationale>"
   ```

3. If `gh` succeeds, end with a decision summary:
   > **Declined** — Issue #<N> closed. Reason: <rationale>. No task created.

4. If `gh` fails, inspect the error output:
   - If the output contains the phrase `already in a 'closed'` (e.g. `already in a 'closed' state`), post the rationale as a standalone comment:
     ```bash
     gh issue comment <number> --repo <owner/repo> --body "Declined: <rationale>"
     ```
     - If the comment succeeds, end with:
       > Issue #<N> is already closed. Reason recorded: <rationale>. No task created.
     - If the comment also fails (e.g. issue is locked), fall through to the manual close URL below, adding a note that the issue is already closed.
   - Otherwise, report the error and show the manual close URL:
     > Could not close issue #<N> automatically. Please close it at: https://github.com/<owner/repo>/issues/<N>
     > Reason to use as a comment: "Declined: <rationale>"

5. **Do NOT insert a task.** Stop — do not proceed to Step 6.

### Defer Path

If the user types **defer**:

1. Proceed to Step 6 to deduplicate and insert the task (same insert flow as the implement-now path). Do NOT call `tusk task-start` or create a branch after insertion.

2. After the task is inserted, attempt to apply the `accepted` label to the GitHub issue so the decision is visible in the issue list without opening tusk:
   ```bash
   gh label list --repo <owner/repo> --json name
   ```
   If `"accepted"` appears in the list, run:
   ```bash
   gh issue edit <number> --repo <owner/repo> --add-label "accepted"
   ```
   If the label does not exist or the command fails, skip silently — labeling is advisory.

3. Post a comment on the GitHub issue:
   ```bash
   gh issue comment <number> --repo <owner/repo> --body "Tracked as tusk task #<task_id>. No timeline yet — will be addressed in a future session."
   ```
   If `gh` fails (e.g. insufficient permissions or locked issue), warn the user:
   > Could not post comment on issue #<N>. Please add it manually: "Tracked as tusk task #<task_id>. No timeline yet — will be addressed in a future session."

4. End with a decision summary:
   > **Deferred** — tusk task #<task_id> created. Issue #<N> commented (and labeled `accepted` if the label exists). No work started yet.

5. **Do NOT proceed to Step 7.** Stop after the comment.

## Step 6: Deduplicate and Insert

Check for semantic duplicates against the backlog from Step 3. If a likely duplicate exists, surface it:

> Possible duplicate: existing task #<id> — "<summary>". Proceed anyway?

If confirmed (or no duplicate found), insert with:

```bash
tusk task-insert "<summary>" "<description>" \
  --priority "<priority>" \
  --domain "<domain>" \
  --task-type "<task_type>" \
  --assignee "<assignee>" \
  --complexity "<complexity>" \
  --criteria "<criterion 1>" \
  --criteria "<criterion 2>" \
  --criteria "<criterion 3>"
```

Omit `--domain` and `--assignee` if NULL. Do not pass empty strings.

**If `test_spec` is set (from Step 4.1)**, append one additional `--typed-criteria` argument to the insert command:

```bash
  --typed-criteria '{"text":"Failing test passes","type":"test","spec":"<test_spec>"}'
```

Replace `<test_spec>` with the extracted command verbatim.

**Single-quote escaping:** If `test_spec` contains a single quote (e.g., a pytest selector like `tests/test_foo.py::test_it's_broken`), the single-quoted JSON wrapper above will break. In that case, assign the spec to a shell variable and use double-quoted outer JSON with escaped inner quotes:

```bash
TEST_SPEC='tests/test_foo.py::test_it'"'"'s_broken'   # use '"'"' to embed a literal single quote
  --typed-criteria "{\"text\":\"Failing test passes\",\"type\":\"test\",\"spec\":\"$TEST_SPEC\"}"
```

When in doubt, always use the variable form — it is safe for any `test_spec` that does not contain a double quote or backslash (which pytest selectors never do).

This criterion will be validated by running the spec as a shell command when `tusk criteria done <cid>` is called — it blocks closure if the command exits nonzero.

**Exit code 0** — success. Note the `task_id` from the JSON output.

**Exit code 1** — heuristic duplicate found. Report the matched task and stop:

> Skipped — duplicate of existing task #<id> (similarity <score>). Run `/tusk <id>` to work on it instead.

**Exit code 2** — error. Report and stop.

## Step 7: Begin Work (Steps 1–11 Only — implement-now path only)

**Skip this step entirely if the user chose defer.** Only proceed here when the user chose confirm (implement now).

Immediately invoke the `/tusk` workflow for the newly created task. Follow the "Begin Work on a Task" instructions from the tusk skill:

```
Read file: <base_directory>/../tusk/SKILL.md
```

Then execute those instructions starting at **"Begin Work on a Task (with task ID argument)"** using the `task_id` from Step 6. Do not wait for additional user confirmation — proceed directly into the development workflow.

**IMPORTANT: Execute /tusk steps 1–11 only. Do NOT execute step 12 (merge/retro).** Stop after step 11 (`/review-commits` or the lint step) — this skill owns merge, issue close, and retro as steps 8–10 below.

Hold onto the `session_id` returned by `tusk task-start` in step 1 of the /tusk workflow — it is required in step 8 below.

## Steps 8–10: Finalize (Run as an Unbroken Sequence — No User Confirmation Between Steps)

### Step 8: Merge

First, check whether work was done on a feature branch or directly on the default branch:

```bash
CURRENT_BRANCH=$(git branch --show-current)
DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
```

If `CURRENT_BRANCH` equals `DEFAULT_BRANCH` (i.e., commits were made directly on main/master), skip `tusk merge` — the commit is already on the default branch. Capture the commit SHA and proceed to Step 9.

If on a feature branch, run the merge normally:

```bash
tusk merge <task_id> --session <session_id>
```

After the merge completes (or if skipped), capture the commit SHA for Step 9:

```bash
git log --oneline -1   # extract the short commit SHA from the first token
```

If the project uses PR-based merges, note the PR URL from the merge output or `gh pr list --state merged --limit 1`.

### Step 9: Close the GitHub Issue

```bash
gh issue close <number> --repo <owner/repo> --comment "Resolved in <commit_sha> — <pr_url_or_branch>. Tracked as tusk task #<task_id>."
```

Use the `commit_sha` from Step 8. If a PR URL is available, include it; otherwise use the branch name.

If the `gh` command fails, inspect the error output:
- If the output contains the phrase `already in a 'closed'` (e.g. `already in a 'closed' state`), post the resolution note as a standalone comment:
  ```bash
  gh issue comment <number> --repo <owner/repo> --body "Resolved in <commit_sha> — <pr_url_or_branch>. Tracked as tusk task #<task_id>."
  ```
  - If the comment succeeds, continue to Step 10 normally.
  - If the comment also fails (e.g. issue is locked), remind the user to add the note manually:
    > Issue #<N> is already closed and locked. Please add the resolution note manually at: https://github.com/<owner/repo>/issues/<N>
- Otherwise (e.g. insufficient permissions), report the error and remind the user to close the issue manually:
  > Could not close issue #<N> automatically. Please close it at: https://github.com/<owner/repo>/issues/<N>

### Step 10: Retro

Invoke `/retro` immediately — do not ask "shall I run retro?". Read and follow:

```
Read file: <base_directory>/../retro/SKILL.md
```
