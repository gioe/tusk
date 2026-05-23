---
name: report-tusk-issue
description: File a GitHub issue against the tusk repo itself — tusk bugs, CLI limitations, skill improvements, or missing features. Use anytime the user identifies a gap in tusk (not in their own project's code).
allowed-tools: Bash, Read
---

# Report Tusk Issue Skill

File a well-formed GitHub issue against the **tusk repo itself** without leaving the workflow. Use this whenever the user (or you) notice a tusk bug, CLI limitation, skill rough-edge, or missing feature mid-session — i.e. a gap in tusk, not in the user's own project.

> Don't use this skill for issues against the *consumer* project's code — those go through whatever issue tracker that project uses. This skill is hard-wired to file against the tusk distribution's repo (`gioe/tusk`).

This skill complements `/retro`'s LR-2 routing — retro still files tusk-issues at end-of-task. This skill is the fast path for noticing a gap *outside* a retro, or during a long task where waiting for retro would lose fidelity.

## Step 1: Gather Inputs from the User

Invoked with an optional one-liner (e.g. `/report-tusk-issue tusk lint rule 14 trips on multi-line skip-lint comments`) or no argument.

Prompt the user for the standard `tusk report-issue` fields. Pre-fill from any one-liner argument, but always confirm before drafting:

1. **Title** (required) — short, imperative. If the user passed a one-liner, treat it as the proposed title and confirm: > Use this as the title? `<one-liner>` (**yes** / edit)
2. **Behavior observed** (required) — what happened, including any error message or unexpected output. Verbatim from the user; don't paraphrase.
3. **Steps to reproduce** (optional) — numbered steps. If the user can't supply them (e.g. it's a UX gap rather than a bug), accept "n/a" and skip.
4. **Expected behavior** (optional) — what should have happened instead. For feature requests, treat this as the proposed behavior.
5. **Project context** (optional) — language, team size, rough task count. **No confidential details.** If the user declines, leave empty — `tusk report-issue` will inline the placeholder comment, which is fine for tusk-team triage.
6. **Cluster** (required) — any `cluster:<name>` label currently present on the repo is accepted; the CLI does not validate against a closed list, so new clusters added to GitHub work immediately. Run `gh label list --repo gioe/tusk --search "cluster:"` to see the current set. Default to `triage-needed` only when no specific cluster fits.

Don't pad missing fields with filler. An empty Steps/Expected section is more honest than invented content; tusk maintainers can ask follow-ups on the issue if needed.

## Step 2: Resolve the Originating Tusk Task (Optional)

Best-effort — try to associate the report with the work that surfaced it so the upstream issue links back to local context:

```bash
TASK_ID=$(tusk branch-parse 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("task_id") or "")' 2>/dev/null)
```

If `$TASK_ID` is non-empty:

```bash
TASK_SUMMARY=$(tusk task-get "$TASK_ID" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("summary",""))' 2>/dev/null)
```

If either step fails (not on a tusk feature branch, or the task is unknown), set both to empty and proceed without a task back-link. Do not prompt the user — most mid-session tusk-issue reports surface during a task, but it's not a hard requirement.

## Step 3: Resolve the Attribution Footer

The footer is what lets tusk maintainers distinguish AI-assisted reports from human-authored ones in triage. Read it from config, falling back to the standard default if the key is unset (older installs predate the key):

```bash
FOOTER=$(tusk config report_tusk_issue_footer 2>/dev/null)
if [[ -z "$FOOTER" ]]; then
  FOOTER="_Filed via /report-tusk-issue from a Claude Code session._"
fi
```

Users who want to override the footer (e.g. to identify their AIQ environment) can set `report_tusk_issue_footer` in `tusk/config.json`. The default is intentionally generic.

## Step 4: Assemble and Show the Draft

Build the final body. The skill submits via `tusk report-issue`, which already wraps the user's inputs in the standard template (`## Tusk version` / `## Project context` / `## Behavior observed` / `## Steps to reproduce` / `## Expected behavior`) and applies the `instance-feedback` label. The skill's job is to (a) collect the inputs cleanly and (b) append the attribution footer + originating-task link.

Build a draft preview that mirrors what `tusk report-issue` will actually post — show the user the assembled body, **not** just the raw inputs. The footer goes after the template body so maintainers see context first:

```
=== Draft GitHub issue against gioe/tusk ===

Title: <title>
Cluster: <cluster>

Body:
## Tusk version
tusk version <integer from `tusk version`>

## Project context
<context or template placeholder>

## Behavior observed
<observed>

## Steps to reproduce
<steps or template placeholder>

## Expected behavior
<expected or template placeholder>

---

<FOOTER>
- Originating tusk task: TASK-<task_id> — <task summary>     # only if Step 2 found one
```

Show the preview, then ask **once** (single prompt — do not re-prompt per-section):

> Open this issue against `gioe/tusk`? (**yes** / edit / cancel)

- `yes` → proceed to Step 5.
- `edit` → loop back to Step 1, pre-filling current values so the user can adjust any field.
- `cancel` → stop. Do not call `tusk report-issue` or `gh`. Do not record progress.

## Step 5: Submit (with Layered Fallback)

The submission has three tiers — each kicks in only if the previous tier fails. **Never auto-retry the same tier**: a failed `gh issue create` could have already partially filed the issue, so blindly retrying risks duplicates.

### Tier 1 — `tusk report-issue`

The primary path. `tusk report-issue` calls `gh issue create --repo gioe/tusk --label instance-feedback --label cluster:<cluster>` internally and exits with the issue URL on stdout:

```bash
if [[ -n "$EXPECTED" ]]; then
  APPENDED_EXPECTED="$EXPECTED

---

$FOOTER"
else
  APPENDED_EXPECTED="$FOOTER"
fi
if [[ -n "$TASK_ID" ]]; then
  APPENDED_EXPECTED="$APPENDED_EXPECTED
- Originating tusk task: TASK-$TASK_ID — $TASK_SUMMARY"
fi

ISSUE_URL=$(tusk report-issue \
  --title "$TITLE" \
  --cluster "$CLUSTER" \
  --context "$CONTEXT" \
  --observed "$OBSERVED" \
  --steps "$STEPS" \
  --expected "$APPENDED_EXPECTED" 2>/tmp/tusk-report-issue.err)
RC=$?
```

The footer is appended to the `--expected` field rather than passed as a separate argument because `tusk report-issue` doesn't accept a `--footer` flag, and the body template renders `## Expected behavior` last — appending here puts the footer at the bottom of the rendered issue, matching the draft preview shown in Step 4. (Appending to `--observed` would wedge the footer between the Behavior and Steps sections — visually wrong.) When the user supplied no expected behavior, the footer stands alone under the heading instead of below a blank-line gap.

If `RC == 0` and `$ISSUE_URL` looks like a GitHub URL (`https://github.com/.../issues/<N>`), proceed to Step 6.

### Tier 2 — Direct `gh issue create`

If Tier 1 failed (non-zero exit), inspect `/tmp/tusk-report-issue.err`. Common reasons `tusk report-issue` could fail while a direct `gh` call still succeeds: an unreleased CLI bug in the report-issue subcommand, a label mismatch, or a tusk-version probe failure. Try the direct call:

```bash
BODY=$(printf '## Project context\n%s\n\n## Behavior observed\n%s\n\n## Steps to reproduce\n%s\n\n## Expected behavior\n%s\n\n---\n\n%s%s' \
  "${CONTEXT:-_(none provided)_}" \
  "$OBSERVED" \
  "${STEPS:-_(none provided)_}" \
  "${EXPECTED:-_(none provided)_}" \
  "$FOOTER" \
  "${TASK_ID:+
- Originating tusk task: TASK-$TASK_ID — $TASK_SUMMARY}")

ISSUE_URL=$(printf '%s' "$BODY" | gh issue create \
  --repo gioe/tusk \
  --label instance-feedback \
  --label cluster:$CLUSTER \
  --title "$TITLE" \
  --body-file - 2>/tmp/gh-issue-create.err)
RC=$?
```

The direct path drops the `## Tusk version` section because that field is sourced inside `tusk report-issue` from `$TUSK_VERSION` — when we're bypassing that path due to a tusk-version probe failure, leaving the section out is more honest than fabricating it. Maintainers will infer from the `instance-feedback` label and the `/report-tusk-issue` footer that the report came from a tusk install.

If `RC == 0`, proceed to Step 6.

### Tier 3 — Manual URL Fallback

If both tiers failed, surface a copy-pasteable manual URL and the assembled body so the user can file by hand. Never silently swallow the failure:

> Could not file the issue automatically. Both `tusk report-issue` and `gh issue create` failed.
>
> **tusk report-issue stderr:** `<contents of /tmp/tusk-report-issue.err>`
> **gh issue create stderr:** `<contents of /tmp/gh-issue-create.err>`
>
> Please open https://github.com/gioe/tusk/issues/new and paste:
>
> **Title:** `<title>`
> **Body:**
> ```
> <assembled body>
> ```
> (Apply the `instance-feedback` and `cluster:<cluster>` labels.)

Then stop — do not record progress.

## Step 6: Record the Issue URL on the Originating Task (If Any)

If Step 2 found `$TASK_ID`, log the URL as a progress checkpoint so it shows up in the local task history:

```bash
tusk progress "$TASK_ID" --next-steps "Filed tusk-issue: $ISSUE_URL"
```

`tusk progress`'s `--next-steps` is the free-form checkpoint field — it does not have to be a literal "next step" — so it doubles as the structured slot for tracking external follow-up.

If Step 2 found no task, skip this step.

## Step 7: Report and Stop

Print the final summary verbatim:

> Filed tusk-issue: `<ISSUE_URL>` (linked on TASK-<task_id>).

Drop the `(linked on …)` suffix if no task was associated.

Do not invoke `/tusk` or `/retro`. Filing a tusk-issue is metadata, not task closure. The user resumes their original workflow when ready.
