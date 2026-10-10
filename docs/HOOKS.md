# Hooks

Tusk ships two parallel guard surfaces. Both are populated by `install.sh` and are intended to enforce the same set of safety rules in the contexts each can reach.

| Surface | Path | Invoked by | Available in |
|---|---|---|---|
| Agent-time | `.claude/hooks/<name>.sh` | Claude Code's `PreToolUse` mechanism (typically gating `Bash`) | Claude Code mode only |
| Git-event  | `hooks/git/<name>.sh` (run via `.git/hooks/<event>` dispatcher) | git itself, on commit/push | Both Claude Code and Codex modes |

The two surfaces overlap deliberately. An agent-time hook can block a hazardous tool call before it ever runs, but it cannot reach a `git commit` that a human types in their own terminal. The git-event hook closes that gap — same rule, different invocation path.

## Per-event guard mapping

`install.sh` writes a fixed mapping of guards to git events:

| Event | Guards |
|-------|--------|
| `pre-commit` | `block-raw-sqlite`, `block-sql-neq`, `dupe-gate`, `scope-guard` |
| `pre-push`   | `branch-naming`, `version-bump-check` |
| `commit-msg` | `commit-msg-format` |

`version-bump-check` guards paths that exist only in the tusk source repo (`bin/`, `skills/`, `config.default.json`, `install.sh`). In a consumer install it would be a silent no-op on every push, so it is omitted from the `pre-push` dispatcher when `INSTALL_ROLE=consumer`.

## Guard contract

Each `hooks/git/<name>.sh` is an independent shell script. The dispatcher invokes it with the same arguments git passed to the dispatcher itself, then inspects the exit code:

- **Exit 0** — guard passed; the dispatcher continues to the next guard.
- **Non-zero** — guard rejected the operation; the dispatcher exits with that same code, which fails the git event.

By convention guards exit `2` on a true rule violation (vs. `1` for an internal error), and they print a one-line `ERROR:` message plus a `bypass with: git <op> --no-verify` hint to stderr. See `hooks/git/branch-naming.sh` for a minimal reference implementation.

## Installation, upgrade, and repair

Fresh installs and upgrades use the same Git-hook installer. It copies the
release guard scripts, resolves Git's effective hook directory (including
`core.hooksPath`), and reconciles executable dispatchers. Relative hook paths
are resolved from the checkout root. Linked worktrees use Git's common hooks
by default; a relative custom hook path instead belongs to each checkout.

```bash
tusk hooks check       # read-only health check; nonzero when missing or stale
tusk hooks install     # repair dispatchers using locally available guard assets
tusk validate          # includes advisory hook health diagnostics
tusk upgrade --force   # restore guard assets from the released distribution
```

A same-version upgrade repairs detected Git-hook drift instead of returning
"Already up to date". Source-repo developers should repair against their local
checkout with `tusk hooks install`; normal consumer upgrades use the release.
Installations made before `git init` retain the assets and print activation
instructions: run `tusk hooks install` after creating the Git repository.

Dispatcher health checks compare generated content, executable bits, and guard
fingerprints. Modified or missing guard assets should be restored with an
upgrade; `hooks install` deliberately uses the currently installed assets.
No hooks are executed during health checks. A hook invocation with missing
required guard scripts fails with a repair diagnostic.

A dispatcher resolves guard assets from the invoking checkout, falling back to
the primary worktree when installed files are absent from a linked checkout.
The selected Tusk binary directory is added to PATH so task-aware guards do
not depend on an interactive shell setup. The source checkout's guard scripts
are preferred over installed mirrors when developing Tusk.

Use a project-specific hook directory. Git shares default hooks across linked
worktrees; a globally configured shared `core.hooksPath` also affects other
repositories. Reconciliation writes the three events in the effective location
and preserves existing user hooks; it does not change Git configuration or
remove dispatchers from a previously configured location.

## Preserving existing hooks

Dispatchers retain the `TUSK_HOOK_DISPATCHER_V1` ownership marker on their second
line. Repeat installs replace owned dispatchers without nesting wrappers.
An existing external hook is renamed to `<event>.pre-tusk`, preserving its
contents, symlink and executable state. If that backup already exists, install
refuses with a collision diagnostic before changing any dispatchers. Resolve
the two external hooks explicitly, then retry; neither is discarded.

Guards run first. The first failure rejects the Git operation. If all guards
pass, an executable user hook runs with the original arguments and its exit
status is returned to Git. For pre-push, the ref-update input is captured once
and replayed in full to every guard and the user hook. Temporary input is
removed on exit. Non-executable user hooks remain inactive.

`tusk uninstall-hooks` resolves the same effective directory, removes owned
dispatchers and restores their preserved user hooks. It leaves external hooks
untouched. Uninstallation leaves guard assets available for later activation.

## Adding a new guard

1. Decide which surface the guard belongs on.
   - **Agent-time only** (e.g., the rule is about a specific tool call sequence Claude makes) → `.claude/hooks/<name>.sh`, wired into `.claude/settings.json` under `hooks.PreToolUse`.
   - **Git-event only** (e.g., the rule is about commit content or branch naming, and must catch human commits too) → `hooks/git/<name>.sh`.
   - **Both** (the common case for safety guards) → write the rule once as a shared shell helper, then thin wrappers in each location.

2. For a git-event guard:
   - Drop `hooks/git/<name>.sh` into the source tree, exit 0 on pass, non-zero (conventionally 2) on violation.
   - Add the guard's basename to `EVENTS` in `bin/tusk-hooks.py` (`pre-commit`, `pre-push`, or `commit-msg`).
   - If the guard targets paths that only exist in the source repo (like `version-bump-check`), filter its inclusion by install role in the shared hook helper.

3. Run `./install.sh` against this repo (or any tusk-installed project) to regenerate the dispatcher. The `TUSK_HOOK_DISPATCHER_V1` marker ensures the existing dispatcher is overwritten in place, and any chained `.pre-tusk` is preserved.

4. Bump the `VERSION` file — `install.sh` and `hooks/git/*.sh` are both delivered to target projects, so changes there require a distribution version bump per the rule in `CLAUDE.md`.

## Bypassing

Any guard can be skipped on a one-shot basis with the standard git escape hatch:

- `git commit --no-verify` — bypasses `pre-commit` and `commit-msg`.
- `git push --no-verify` — bypasses `pre-push`.

`tusk commit --skip-verify` does the same for tusk-driven commits. `tusk merge --skip-lint` skips only the pre-merge lint gate added by TASK-586 for GitHub issue #996. `tusk merge --skip-verify` is the broader bypass: today it skips that same lint gate, and it is reserved to skip future pre-merge verification gates as they are added.
