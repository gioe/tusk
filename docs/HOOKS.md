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
| `pre-commit` | `block-raw-sqlite`, `block-sql-neq`, `dupe-gate` |
| `pre-push`   | `branch-naming`, `version-bump-check` |
| `commit-msg` | `commit-msg-format` |

`version-bump-check` guards paths that exist only in the tusk source repo (`bin/`, `skills/`, `config.default.json`, `install.sh`). In a consumer install it would be a silent no-op on every push, so it is omitted from the `pre-push` dispatcher when `INSTALL_ROLE=consumer`.

## Guard contract

Each `hooks/git/<name>.sh` is an independent shell script. The dispatcher invokes it with the same arguments git passed to the dispatcher itself, then inspects the exit code:

- **Exit 0** — guard passed; the dispatcher continues to the next guard.
- **Non-zero** — guard rejected the operation; the dispatcher exits with that same code, which fails the git event.

By convention guards exit `2` on a true rule violation (vs. `1` for an internal error), and they print a one-line `ERROR:` message plus a `bypass with: git <op> --no-verify` hint to stderr. See `hooks/git/branch-naming.sh` for a minimal reference implementation.

## The `.git/hooks/<event>` dispatcher

`install.sh` writes one dispatcher per git event (`pre-commit`, `pre-push`, `commit-msg`) into `.git/hooks/`. The dispatcher:

1. Carries a `TUSK_HOOK_DISPATCHER_V1` marker comment on the second line.
2. Resolves `HOOKS_DIR` to whichever of `.claude/bin/hooks/git` or `tusk/bin/hooks/git` exists for the install mode.
3. Runs each guard in the mapping above. On the first non-zero exit, the dispatcher exits with that code and skips the rest.
4. Finally `exec`s `.git/hooks/<event>.pre-tusk` if one exists (see "Chaining external hooks" below).
5. Otherwise exits `0`.

### `TUSK_HOOK_DISPATCHER_V1` — idempotent re-runs

The `TUSK_HOOK_DISPATCHER_V1` marker on line 2 is how `install.sh` recognises its own dispatchers on subsequent runs. Without it, every `tusk upgrade` (or repeated `install.sh` invocation) would re-discover the dispatcher as a "user hook" and chain it to `.pre-tusk`, eventually nesting tusk dispatchers inside each other. The marker is grepped for via:

```bash
grep -q "TUSK_HOOK_DISPATCHER_V1" .git/hooks/<event>
```

When that grep returns 0 the existing dispatcher is overwritten in place; when it returns non-zero the existing file is treated as an external user hook and chained.

Bumping the version suffix (e.g. to `_V2`) in the future would force an explicit migration step, since older marker matches would no longer succeed.

### Chaining external hooks via `<event>.pre-tusk`

A user can have a pre-existing hook at `.git/hooks/<event>` (their own pre-commit linter, an enterprise-mandated audit hook, etc.). The dispatcher must run AFTER the tusk guards but the user's hook should still fire. Resolution:

1. On install, if `.git/hooks/<event>` exists AND does not carry the `TUSK_HOOK_DISPATCHER_V1` marker AND no `.pre-tusk` already exists, the file is renamed to `.git/hooks/<event>.pre-tusk` (once).
2. The dispatcher is then written to `.git/hooks/<event>`.
3. The final line of the dispatcher is `exec "$HERE/<event>.pre-tusk" "$@"` (if executable), which hands control to the chained hook with the original arguments. The user's hook can still reject the operation by exiting non-zero — that exit code propagates back to git.

The "once" guard on the rename means re-running `install.sh` against an already-tusk-managed repo never double-renames: the marker check on the dispatcher fails the "is external" condition, the rename branch is skipped, and the existing `.pre-tusk` is preserved.

## Adding a new guard

1. Decide which surface the guard belongs on.
   - **Agent-time only** (e.g., the rule is about a specific tool call sequence Claude makes) → `.claude/hooks/<name>.sh`, wired into `.claude/settings.json` under `hooks.PreToolUse`.
   - **Git-event only** (e.g., the rule is about commit content or branch naming, and must catch human commits too) → `hooks/git/<name>.sh`.
   - **Both** (the common case for safety guards) → write the rule once as a shared shell helper, then thin wrappers in each location.

2. For a git-event guard:
   - Drop `hooks/git/<name>.sh` into the source tree, exit 0 on pass, non-zero (conventionally 2) on violation.
   - Add the guard's basename to the appropriate `write_dispatcher` call in `install.sh` (`pre-commit`, `pre-push`, or `commit-msg`).
   - If the guard targets paths that only exist in the source repo (like `version-bump-check`), gate the inclusion behind the `INSTALL_ROLE=consumer` branch in `install.sh`.

3. Run `./install.sh` against this repo (or any tusk-installed project) to regenerate the dispatcher. The `TUSK_HOOK_DISPATCHER_V1` marker ensures the existing dispatcher is overwritten in place, and any chained `.pre-tusk` is preserved.

4. Bump the `VERSION` file — `install.sh` and `hooks/git/*.sh` are both delivered to target projects, so changes there require a distribution version bump per the rule in `CLAUDE.md`.

## Bypassing

Any guard can be skipped on a one-shot basis with the standard git escape hatch:

- `git commit --no-verify` — bypasses `pre-commit` and `commit-msg`.
- `git push --no-verify` — bypasses `pre-push`.

`tusk commit --skip-verify` does the same for tusk-driven commits, and `tusk commit --skip-lint` skips only the lint step without bypassing the git hooks. Use sparingly — every guard exists because something hit production once.
