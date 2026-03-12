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

## Step 5: Present Proposed Task for Review

Show the proposed task in compact format:

```markdown
## Proposed Task from Issue #<N>

**<summary>** (<priority> · <domain> · <task_type> · <complexity>)
> <description preview — first 2 sentences>

**Acceptance Criteria:**
1. <criterion 1>
2. <criterion 2>
...
```

Then ask:

> Create this task? You can **confirm**, **edit** (e.g., "change priority to High"), **decline** (close the issue without creating a task), or **cancel**.

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
   - If the output contains `already` and `closed` (e.g. `already in a 'closed' state`), treat it as a success:
     > Issue #<N> is already closed. Reason recorded: <rationale>. No task created.
   - Otherwise, report the error and show the manual close URL:
     > Could not close issue #<N> automatically. Please close it at: https://github.com/<owner/repo>/issues/<N>
     > Reason to use as a comment: "Declined: <rationale>"

5. **Do NOT insert a task.** Stop — do not proceed to Step 6.

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

**Exit code 0** — success. Note the `task_id` from the JSON output.

**Exit code 1** — heuristic duplicate found. Report the matched task and stop:

> Skipped — duplicate of existing task #<id> (similarity <score>). Run `/tusk <id>` to work on it instead.

**Exit code 2** — error. Report and stop.

## Step 7: Begin Work (Steps 1–11 Only)

Immediately invoke the `/tusk` workflow for the newly created task. Follow the "Begin Work on a Task" instructions from the tusk skill:

```
Read file: <base_directory>/../tusk/SKILL.md
```

Then execute those instructions starting at **"Begin Work on a Task (with task ID argument)"** using the `task_id` from Step 6. Do not wait for additional user confirmation — proceed directly into the development workflow.

**IMPORTANT: Execute /tusk steps 1–11 only. Do NOT execute step 12 (merge/retro).** Stop after step 11 (`/review-commits` or the lint step) — this skill owns merge, issue close, and retro as steps 8–10 below.

Hold onto the `session_id` returned by `tusk task-start` in step 1 of the /tusk workflow — it is required in step 8 below.

## Steps 8–10: Finalize (Run as an Unbroken Sequence — No User Confirmation Between Steps)

### Step 8: Merge

```bash
tusk merge <task_id> --session <session_id>
```

After the merge completes, capture the commit SHA for Step 9:

```bash
git log --oneline -1   # extract the short commit SHA from the first token
```

If the project uses PR-based merges, note the PR URL from the merge output or `gh pr list --state merged --limit 1`.

### Step 9: Close the GitHub Issue

```bash
gh issue close <number> --repo <owner/repo> --comment "Resolved in <commit_sha> — <pr_url_or_branch>. Tracked as tusk task #<task_id>."
```

Use the `commit_sha` from Step 8. If a PR URL is available, include it; otherwise use the branch name.

If the `gh` command fails (e.g. insufficient permissions), report the error and remind the user to close the issue manually:

> Could not close issue #<N> automatically. Please close it at: https://github.com/<owner/repo>/issues/<N>

### Step 10: Retro

Invoke `/retro` immediately — do not ask "shall I run retro?". Read and follow:

```
Read file: <base_directory>/../retro/SKILL.md
```
