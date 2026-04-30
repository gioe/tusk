---
name: ios-libs-issue
description: File a GitHub issue against the configured iOS lib repo with originating tusk task auto-attached
allowed-tools: Bash, Read
applies_to_project_types: [ios_app]
---

# iOS Libs Issue Skill

File a well-formed GitHub issue against the iOS lib repo configured in `tusk/config.json` (`project_libs.ios_app.repo`) without leaving the workflow. The originating tusk task summary, tusk version, and a link back to the local task are auto-attached so the upstream issue stays connected to the work that surfaced it.

This skill is content-only — there are no `bin/tusk` hooks to invoke. Adding the same flow for another platform (e.g. `android-libs-issue`) is a copy-edit job that touches only this file's sibling.

## Step 1: Resolve the Originating Local Task

The skill is invoked from a tusk client repo while the user is in the middle of work that surfaced a bug or gap in the iOS lib. Determine the originating task ID:

1. If the user invoked `/ios-libs-issue <id>` with an explicit ID, use it.
2. Otherwise parse it from the current branch:
   ```bash
   TASK_ID=$(tusk branch-parse | python3 -c 'import json,sys; print(json.load(sys.stdin).get("task_id") or "")')
   ```
3. If neither produces a task ID, prompt:
   > No tusk task detected from `/ios-libs-issue <id>` or the current branch. Provide a task ID, or type `cancel` to abort.

Once the ID is in hand, fetch the task so its summary can be embedded in the issue body:

```bash
tusk task-get "$TASK_ID"
```

Record `task.summary` and `task.id` for Step 4.

## Step 2: Resolve the iOS Lib Repo from Config

The lib repo is **never hard-coded**. Read it from `project_libs.ios_app.repo` at runtime:

```bash
PROJECT_TYPE=$(tusk config project_type)
LIB_REPO=$(tusk config "project_libs.${PROJECT_TYPE}.repo")
LIB_REF=$(tusk config "project_libs.${PROJECT_TYPE}.ref")
```

Validate:
- If `PROJECT_TYPE` is empty or not `ios_app` → stop with `> /ios-libs-issue requires project_type=ios_app in tusk/config.json. Run /tusk-update to set it.` (The `applies_to_project_types: [ios_app]` frontmatter normally prevents install on non-iOS projects, but a stale install or a manually re-typed project may still reach this step.)
- If `LIB_REPO` is empty → stop with `> project_libs.ios_app.repo is unset in tusk/config.json. Run /tusk-update to configure it.`

`LIB_REF` is optional — surface it in the issue body so the upstream maintainer knows which ref the client is pinned to.

## Step 3: Choose Issue Type and Gather Inputs

Ask the user which template to use:

> File which kind of issue? (**bug** / **feature**, default: bug)

Treat any non-`feature` answer as `bug`. Then prompt for:

1. **Title** (required) — keep it under ~80 chars, imperative for features (`Add APIClient retry policy`), descriptive for bugs (`SharedKit Color.surface returns wrong hex on iOS 17`).
2. **Body** — paste or type the issue body using the template that matches the chosen type. The templates below are starting points; the user can edit freely before submitting.
3. **Labels** (optional) — comma-separated list (e.g. `bug,priority:high`). Skip if the lib repo doesn't use a labeling convention.

### Bug template

```markdown
## Summary
<one-sentence description of the bug>

## Reproduction
1. <step>
2. <step>
3. <step>

## Expected
<what should happen>

## Actual
<what happens instead, including any error output>

## Environment
- iOS version:
- Xcode version:
- Lib ref: <auto-filled in Step 4 from project_libs.ios_app.ref>
```

### Feature template

```markdown
## Problem
<what is hard or impossible to do today>

## Proposal
<what the lib could provide to solve it>

## Alternatives considered
<approaches that were tried or rejected>

## Additional context
<links, screenshots, related issues>
```

## Step 4: Assemble the Final Body with Auto-Attached Context

Append a footer to the user's body so the upstream issue is linked back to the originating local task. Capture the tusk version with `tusk version` (it prints `tusk version <N>` — extract the integer).

```markdown
<user-supplied body, with `Lib ref:` filled in if the bug template was used>

---

_Filed via `/ios-libs-issue` from a tusk client._
- Originating tusk task: TASK-<task_id> — <task.summary>
- tusk version: <integer from `tusk version`>
- Lib ref pinned by client: <LIB_REF or "unspecified">
```

The originating task ID and summary are the load-bearing back-link — without them, an upstream maintainer reading the issue has no way to reach the conversation that surfaced it. Do not omit them even if the user's body already references the task.

## Step 5: Show the Final Payload and Confirm

Display the assembled title, body, and labels exactly as they will be sent. Ask:

> Open this issue against `<LIB_REPO>`? (**yes** / **edit** / **cancel**)

- `yes` → proceed to Step 6.
- `edit` → loop back to Step 3 with the current values pre-filled so the user can adjust.
- `cancel` → stop. Do not call `gh`. Do not record progress.

## Step 6: Create the Issue

Pass the body via stdin to avoid quoting hazards (issue bodies routinely contain backticks, quotes, and `$` characters that the shell would otherwise interpret):

```bash
ISSUE_URL=$(printf '%s' "$ISSUE_BODY" | gh issue create \
  --repo "$LIB_REPO" \
  --title "$ISSUE_TITLE" \
  --body-file - \
  ${LABELS:+--label "$LABELS"})
```

If `gh` exits non-zero:

1. If stderr contains `authentication required` or `gh auth login`, surface: > `gh` is not authenticated against `<LIB_REPO>`. Run `gh auth login` and retry `/ios-libs-issue`.
2. If stderr contains `Could not resolve to a Repository`, the configured `project_libs.ios_app.repo` is wrong or the user lacks access. Surface the repo name and suggest `/tusk-update`.
3. Otherwise surface the raw `gh` stderr and stop. Do not retry automatically — repeated `gh issue create` calls would file duplicate issues.

On success, `$ISSUE_URL` holds the new issue's URL.

## Step 7: Record the Issue URL on the Originating Task

Log the URL as a progress checkpoint on the originating task so it shows up in the local task history:

```bash
tusk progress "$TASK_ID" --next-steps "Filed upstream issue against $LIB_REPO: $ISSUE_URL"
```

`tusk progress`'s `--next-steps` is the free-form checkpoint field — it does not have to be a literal "next step" — so it doubles as the structured slot for tracking external follow-up.

## Step 8: Report and Stop

Print the final summary verbatim:

> Filed issue against `<LIB_REPO>`: `<ISSUE_URL>` (linked on TASK-<task_id>).

Do not invoke `/tusk` or `/retro`. The originating task remains in whatever status it was in — filing an upstream issue is metadata, not task closure. The user resumes their original workflow (typically `/tusk` on the originating task) when ready.
