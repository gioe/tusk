# Conditional recovery

- Missing task: report the exact error and inspect the nearby backlog; never create
  a substitute task silently.
- Existing session/workspace: use `task-get` progress and `task-worktree list`; resume
  the owning workspace. Do not force-attach to another agent's session by default.
- Failed check: inspect its output, fix failures caused by this change, rerun affected
  checks. Existing failures remain explicit and need the project's resolution policy.
- Scope expansion: use `tusk scope expand <id> <path> --reason <why>` for intentional
  changes, preserving the task's objective.
- Version bump staging failure: inspect VERSION before retrying; the file may already
  have incremented. Stage the existing change instead of bumping twice.
- Merge interruption: inspect task and branch state and retry the same `tusk merge`
  with the original session ID. Do not reset branches or discard dirty work to clean up.
- Missing native skill/resource: `tusk skills check`, then `tusk skills install` repairs
  unchanged/missing managed files using the installed bundle. Custom conflicts remain
  untouched; compare against the bundle and decide which version to keep.
