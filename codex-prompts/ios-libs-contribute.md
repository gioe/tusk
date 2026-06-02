# iOS Libs Contribute

Contribute a client-discovered fix or improvement back to the iOS library repo configured in the current tusk project. Use this only for `project_type=ios_app`.

## Step 1: Resolve the Originating Task

Use the explicit `/ios-libs-contribute <id>` argument when present. Otherwise parse the task from the current branch:

```bash
TASK_ID=$(tusk branch-parse | python3 -c 'import json,sys; print(json.load(sys.stdin).get("task_id") or "")')
```

Fetch the task with `tusk task-get "$TASK_ID"` and keep its summary for the PR body.

## Step 2: Resolve the Configured Lib Repo

Do not hard-code the library repo. Read it from tusk config:

```bash
PROJECT_TYPE=$(tusk config project_type)
LIB_REPO=$(tusk config "project_libs.${PROJECT_TYPE}.repo")
LIB_REF=$(tusk config "project_libs.${PROJECT_TYPE}.ref")
```

Stop if `PROJECT_TYPE` is not `ios_app` or `LIB_REPO` is empty.

## Step 3: Prepare the Library Workspace

Ask which local files or behavior should be contributed upstream, inspect the local diff, and summarize what will be ported.

Create a temp parent outside the client repo, then fork and clone the configured repo by default:

```bash
LIB_WORKSPACE_PARENT=$(mktemp -d)
cd "$LIB_WORKSPACE_PARENT"
gh repo fork "$LIB_REPO" --clone --remote
```

If a clone already exists, reuse it only after `git status --short` shows a clean tree. Create a branch named:

```text
tusk/<task_id>-<slug>
```

## Step 4: Port and Verify

Copy or re-implement only the library-relevant change. Run the library's own test suite. Prefer documented commands; common options are `swift test` or `xcodebuild test` with the repo's scheme and simulator destination.

## Step 5: Commit, Push, and Open the PR

Commit in the library repo, push the branch, and create a draft PR:

```bash
git add <changed-files>
git commit -m "Contribute TASK-${TASK_ID} fix"
git push -u origin "tusk/<task_id>-<slug>"
gh pr create --repo "$LIB_REPO" --draft --title "<title>" --body-file -
```

Pass the PR body via stdin. Include summary, verification, `Originating tusk task: TASK-<task_id> - <task.summary>`, and `Client lib ref: <LIB_REF or "unspecified">`.

## Step 6: Record Progress

When `gh pr create` returns a URL, record it on the originating task:

```bash
tusk progress "$TASK_ID" --note "Opened upstream PR against $LIB_REPO: $PR_URL"
```

Report:

```text
Opened upstream PR against <LIB_REPO>: <PR_URL> (linked on TASK-<task_id>).
```
