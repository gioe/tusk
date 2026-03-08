---
name: address-issue
description: Fetch a GitHub issue, create a tusk task from it, and work through it with /tusk
allowed-tools: Bash, Read, Edit, Write, Grep, Glob
---

# Address Issue Skill

Fetches a GitHub issue, converts it into a tusk task, and immediately begins working on it using the full `/tusk` workflow.

## Step 1: Parse the Issue Reference

The user invokes this skill with an issue number or full URL. Examples:
- `/address-issue 314`
- `/address-issue https://github.com/gioe/tusk/issues/314`

Extract the issue number. If a full URL is given, parse the number from the path. If no argument was provided, ask:

> Which GitHub issue should I address? Provide an issue number (e.g. `314`) or a full URL.

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

> Create this task? You can **confirm**, **edit** (e.g., "change priority to High"), or **cancel**.

Wait for explicit user approval before inserting.

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

Capture the JSON output. Extract:
- `commit_sha` — the merge commit hash
- `pr_url` — the pull request URL (if a PR was created), or `null`

### Step 9: Close the GitHub Issue

```bash
gh issue close <number> --repo <owner/repo> --comment "Resolved in <commit_sha> — <pr_url_or_branch>. Tracked as tusk task #<task_id>."
```

Use `commit_sha` and `pr_url` from Step 8. If `pr_url` is null, omit the PR reference and use the branch name instead.

If the `gh` command fails (e.g. insufficient permissions), report the error and remind the user to close the issue manually:

> Could not close issue #<N> automatically. Please close it at: https://github.com/<owner/repo>/issues/<N>

### Step 10: Retro

Invoke `/retro` immediately — do not ask "shall I run retro?". Read and follow:

```
Read file: <base_directory>/../retro/SKILL.md
```
