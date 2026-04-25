# Codex Compatibility

tusk supports installation in both Claude Code projects (the original target) and Codex projects. `install.sh` auto-detects which layout is present and adapts the install tree accordingly. This doc explains how Codex mode works and what differs from Claude mode.

## Agent-mode detection

`install.sh` picks the mode from the host project's scaffolding in this order:

1. `.claude/` directory present → **claude** mode (unchanged default).
2. `AGENTS.md` present and no `.claude/` → **codex** mode.
3. Neither present → hard error with a message pointing at both supported markers.

The chosen mode is persisted to `<install_dir>/install-mode` (contents: `claude` or `codex`). `bin/tusk` reads this marker to route gitignore and agent-doc writes; `bin/tusk-upgrade.py` reads it to decide which steps to run during an upgrade.

## What Codex mode installs

| Component              | Claude mode                     | Codex mode                       |
| ---------------------- | ------------------------------- | -------------------------------- |
| Binaries + Python libs | `.claude/bin/`                  | `tusk/bin/`                      |
| Config & pricing       | `.claude/bin/config.default.json`, `.claude/bin/pricing.json` | `tusk/bin/config.default.json`, `tusk/bin/pricing.json` |
| Install-mode marker    | `.claude/bin/install-mode`      | `tusk/bin/install-mode`          |
| Manifest               | `.claude/tusk-manifest.json`    | `tusk/tusk-manifest.json`        |
| Skills                 | `.claude/skills/<name>/`        | Not installed (no Codex primitive) |
| Codex prompts          | Not installed (no Claude primitive) | `.codex/prompts/<name>.md` |
| Hooks                  | `.claude/hooks/<name>`          | Not installed (no Codex primitive) |
| Settings merge         | `.claude/settings.json`         | Not performed (no file to merge into) |
| Agent-doc update       | `CLAUDE.md` (created if absent) | `AGENTS.md` (created if absent)  |
| `.gitignore` entries   | `.claude/bin/`, `.claude/settings.json`, `.claude/tusk-manifest.json`, `tusk/tasks.db*`, `tusk/backups/` | `tusk/bin/`, `tusk/tusk-manifest.json`, `tusk/tasks.db*`, `tusk/backups/` |

The `tusk/tasks.db` database and `tusk/config.json` live in the same location in both modes — only the binaries and manifest move.

## PATH setup in Codex mode

Claude mode installs a `setup-path.sh` hook that prepends `.claude/bin/` to `PATH` for every Claude Code session. Codex has no equivalent hook system, so Codex users must wire up `PATH` themselves. Two common options:

- **Shell profile** — add `export PATH="$(git rev-parse --show-toplevel)/tusk/bin:$PATH"` to your zsh/bash profile when inside the project directory.
- **direnv** — create `.envrc` at the repo root with `PATH_add tusk/bin` and `direnv allow`.

`install.sh` prints a reminder with the exact `export PATH=...` line after a successful Codex install.

## Feature parity

The Claude-only components (skills, hooks, settings merge, `/review-commits` permissions) are skipped in Codex mode because Codex has no primitives for them. That means Codex users do not get:

- The `/tusk`, `/create-task`, `/groom-backlog`, `/retro`, `/chain`, `/loop`, `/review-commits`, `/tusk-init`, `/tusk-update`, `/tusk-insights`, `/investigate`, `/investigate-directory`, `/resume-task`, `/address-issue` skills — these are Claude Code slash commands that only load when `.claude/skills/` is populated.
- The PreToolUse/PostToolUse hooks (e.g. `conventions-preflight.sh`) that fire on tool invocations.
- The permissions allowlist entries required for `/review-commits`.

Everything else works identically: the `tusk` CLI, task database, criteria tracking, workflows, migrations, and `tusk upgrade`.

## Upgrading a Codex install

`tusk upgrade` works the same way in both modes — it downloads the latest tarball, copies files into the install dir resolved from `$SCRIPT_DIR`, and runs `tusk migrate`. In Codex mode it additionally:

1. Reads `install-mode` from the install dir and translates the tarball's `MANIFEST` to the local `tusk/bin/` layout before running orphan detection. The translation rewrites `.claude/bin/` → `tusk/bin/`, drops `.claude/skills/` and `.claude/hooks/` entries (no Codex equivalents), and **keeps `.codex/prompts/*.md` entries** so prompts ship in codex mode. Claude-mode upgrades inversely drop `.codex/prompts/` so those files don't land where there's no consumer.
2. Skips the skills copy, hooks copy, `setup-path.sh` override, settings merge, and `/review-commits` permissions check.
3. Copies `codex-prompts/*.md` from the tarball to `<repo_root>/.codex/prompts/<name>.md` (Codex-only step; mirrors how Claude mode copies `skills/`).
4. Writes the translated manifest to `tusk/tusk-manifest.json` so future upgrades see the correct baseline.

## Limitations

- Migrating an existing Claude install to Codex (or vice versa) is not supported. Remove the old install dir, delete the install-mode marker, and re-run `install.sh`.

## First-time setup (Codex)

Use `tusk init-wizard` to configure `tusk/config.json` (domains, agents, task types, test command, project type, and optional bootstrap task seeding). The wizard is the Codex-facing equivalent of the Claude-only `/tusk-init` skill — it runs in any shell and works the same way in Claude and Codex sessions.

- **Interactive** (default when stdin is a TTY): prompts for each setting with suggestions derived from a codebase scan.

  ```bash
  tusk init-wizard
  ```

- **Non-interactive** (flags only, scripting-safe):

  ```bash
  tusk init-wizard --non-interactive \
    --domains '["api","frontend"]' \
    --agents '{"backend":"APIs and DB","frontend":"UI"}' \
    --task-types '["bug","feature","docs"]' \
    --test-command 'pytest -q' \
    --project-type python_service
  ```

Passing `--project-type ios_app` or `--project-type python_service` auto-populates `project_libs` from `config.default.json`. Add `--seed-bootstrap-tasks all` to fetch each lib's `tusk-bootstrap.json` and insert the published tasks in one pass.
