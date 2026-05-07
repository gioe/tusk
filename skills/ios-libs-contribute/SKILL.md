---
name: ios-libs-contribute
description: Contribute a client-discovered fix or improvement back to the configured iOS library repo from an ios_app tusk project.
allowed-tools: Bash, Read, Edit
applies_to_project_types: [ios_app]
---

# iOS Libs Contribute Skill

Open a pull request against the iOS library repo configured by the current tusk client project, while keeping the upstream work linked to the originating local task. Use a fork-based PR by default because most client projects will not have direct push access to the shared library repo.

## Step 1: Resolve the Originating Local Task

Determine the local task that surfaced the library change:

1. If the user invoked `/ios-libs-contribute <id>`, use that task ID.
2. Otherwise parse it from the current branch:
   ```bash
   TASK_ID=$(tusk branch-parse | python3 -c 'import json,sys; print(json.load(sys.stdin).get("task_id") or "")')
   ```
3. If neither produces a task ID, ask for one or stop.

Fetch the task:

```bash
tusk task-get "$TASK_ID"
```

Record `task.summary` for the PR body and progress note.

## Step 2: Resolve the Configured iOS Lib Repo

Never hard-code the library repo. Read it from `project_libs.<project_type>.repo`:

```bash
PROJECT_TYPE=$(tusk config project_type)
LIB_REPO=$(tusk config "project_libs.${PROJECT_TYPE}.repo")
LIB_REF=$(tusk config "project_libs.${PROJECT_TYPE}.ref")
```

Validate:
- If `PROJECT_TYPE` is not `ios_app`, stop with: `/ios-libs-contribute requires project_type=ios_app in tusk/config.json.`
- If `LIB_REPO` is empty, stop with: `project_libs.ios_app.repo is unset in tusk/config.json.`

`LIB_REF` is optional. Mention it in the PR body when present.

## Step 3: Confirm the Upstream Change

Ask the user to identify the local change that should be moved upstream:

```text
Which local files or behavior should be contributed to <LIB_REPO>?
```

Before editing the library repo, inspect the local diff and summarize exactly what will be ported. If the change is ambiguous, ask for clarification. Do not copy unrelated client project code or secrets into the library repo.

## Step 4: Prepare a Fork-Based Library Workspace

Use a sibling or temp workspace outside the client repo. Change into that parent before cloning so the fork is not created inside the client project:

```bash
LIB_WORKSPACE_PARENT=$(mktemp -d)
cd "$LIB_WORKSPACE_PARENT"
```

Fork first so the branch can be pushed even when the user lacks write access:

```bash
gh repo fork "$LIB_REPO" --clone --remote
```

If the repo is already cloned, reuse it after confirming it is clean:

```bash
git status --short
```

Create the upstream work branch:

```bash
git switch -c "tusk/<task_id>-<slug>"
```

Use a short lowercase slug from the local task summary. Keep the literal branch shape `tusk/<task_id>-<slug>`.

## Step 5: Port the Change

Copy or re-implement the selected client change into the library workspace. Keep the PR focused on library code and tests only.

After editing:

```bash
git diff --stat
git diff
```

Confirm the diff contains only the intended upstream contribution.

## Step 6: Run the Library Test Suite

Detect the library's test command from its own repo files. Prefer the repo's documented test suite command. Common iOS library checks include:

```bash
swift test
xcodebuild test -scheme <scheme> -destination 'platform=iOS Simulator,name=<device>'
```

If no test command is discoverable, explain what was checked manually and why automated verification was unavailable.

## Step 7: Commit, Push, and Open the PR

Commit in the library repo:

```bash
git add <changed-files>
git commit -m "Contribute TASK-${TASK_ID} fix"
git push -u origin "tusk/<task_id>-<slug>"
```

Open a draft PR by default:

```bash
gh pr create --repo "$LIB_REPO" --draft --title "<title>" --body-file -
```

Pass the body via stdin. Include:

```markdown
## Summary
<what changed>

## Verification
- <test command and result>

## Originating tusk task
TASK-<task_id> - <task.summary>

## Client lib ref
<LIB_REF or "unspecified">
```

If `gh pr create` fails due to authentication or repository access, surface the exact stderr and stop. Do not retry automatically because a PR may have been partially created.

## Step 8: Record the Upstream PR on the Local Task

When the PR URL is available, record it on the originating local task:

```bash
tusk progress "$TASK_ID" --next-steps "Opened upstream PR against $LIB_REPO: $PR_URL"
```

This keeps the external contribution visible in local task history.

## Step 9: Report and Stop

Print:

```text
Opened upstream PR against <LIB_REPO>: <PR_URL> (linked on TASK-<task_id>).
```

Do not close the originating task automatically. The upstream PR may need review before the client work can finish.
