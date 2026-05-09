# Issue Cluster Labels Design

## Goal

Make tusk GitHub issues easier to batch by requiring one `cluster:*` label before backlog work starts, while keeping issue filing lightweight.

## Taxonomy

Use a small fixed label set:

- `cluster:worktree`
- `cluster:merge`
- `cluster:review-diff`
- `cluster:summary`
- `cluster:docs`
- `cluster:test-precheck`
- `cluster:small-fix`
- `cluster:triage-needed`

`cluster:triage-needed` is the default escape hatch. Grooming should replace it with a more specific cluster or close the issue as a duplicate.

## Filing Paths

The GitHub issue template should include a required dropdown so manual reporters pick a cluster. The default label list should include `instance-feedback` and `cluster:triage-needed`, because GitHub issue forms cannot map dropdown choices to labels without automation.

The `tusk report-issue` CLI should accept `--cluster <name>` and apply `--label cluster:<name>` alongside `instance-feedback`. When omitted, it should use `cluster:triage-needed`. Invalid values should fail before invoking `gh`.

The `/report-tusk-issue` skill should ask the agent or user to pick a cluster and pass it through to `tusk report-issue`. Its direct `gh issue create` fallback should apply the same cluster label.

## Acceptance

- `tusk report-issue --dry-run` shows both `instance-feedback` and the chosen `cluster:*` label.
- `tusk report-issue --cluster nonsense --dry-run` exits nonzero with the allowed cluster list.
- The issue template requires a cluster dropdown and defaults new feedback issues to `cluster:triage-needed`.
- The report skill documents the cluster prompt and pass-through.
