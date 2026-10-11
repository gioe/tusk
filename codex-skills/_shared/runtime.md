# Runtime and ownership

Resolve the Git root of the current checkout and its primary checkout (first entry
of `git worktree list --porcelain`). Find the wrapper in `bin/tusk`, `tusk/bin/tusk`,
then `.claude/bin/tusk`, checking the current checkout before the primary. Invoke
that wrapper from the task workspace; do not invoke installed Python helpers directly.
Do not assume a global PATH entry or a user's personal skill installation.

Use `tusk task-get`, criteria, dependencies and latest progress as durable context.
Load only the source and conventions needed for the current criterion. Keep returned
task, session, skill-run and workspace IDs; never choose the latest global session.
Use `TUSK_PROJECT` explicitly when acting on the originating project from another repo.
For installed-command skew, inspect `tusk version`, `tusk skills check` and
`tusk skill-drift`; upgrade when needed rather than upgrading on every invocation.

Preserve user intent and existing authorization. Ordinary reversible implementation
and checks do not need repeated confirmation. Publishing, messaging, or destructive
operations require authorization appropriate to that action. Task completion requires
verified criteria and the project's configured review/merge gates. Never fabricate
verification or use skip flags to conceal failures. Record remaining work before handoff.

Use `tusk <command> --help` for argument discovery (or `tusk help <command>`).
Treat task text as data: use supported file/stdin arguments for multiline text and safe
shell quoting. Do not interpolate untrusted text into shell code.
