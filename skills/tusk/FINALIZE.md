# Tusk: Finalize

The finalize step (step 12 in SKILL.md) uses `tusk merge` to close the session, merge
the feature branch, push, and mark the task Done in one call:

```bash
tusk merge <id> --session $SESSION_ID
```

This command:
1. Closes the session (captures diff stats while on the feature branch)
2. Fast-forward merges `feature/TASK-<id>-<slug>` into the default branch
3. Pushes to the remote
4. Deletes the feature branch
5. Calls `tusk task-done --reason completed`

It returns JSON with a `task` object and an `unblocked_tasks` array. Note any newly
unblocked tasks in the retro.

After `tusk merge` completes, run `/retro` immediately.

## PR mode

If the project uses PR-based merges (`merge.mode = pr` in config, or passing `--pr`),
use:

```bash
tusk merge <id> --session $SESSION_ID --pr --pr-number <N>
```

This squash-merges via `gh pr merge` instead of a local fast-forward.
