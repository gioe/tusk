# Codex Compatibility

tusk supports installation in both Claude Code projects (the original target) and Codex projects. `install.sh` auto-detects which layout is present and adapts the install tree accordingly. This doc explains how Codex mode works and what differs from Claude mode.

## Agent-mode detection

`install.sh` picks the mode from the host project's scaffolding in this order:

1. `.claude/` directory present → **claude** mode (unchanged default).
2. `AGENTS.md` present and no `.claude/` → **codex** mode.
3. Neither present → hard error with a message pointing at both supported markers.

The chosen mode is persisted to `<install_dir>/install-mode` in the compound form `<mode>-<role>` (e.g. `claude-source`, `claude-consumer`, `codex-consumer`). `bin/tusk` reads this marker to route gitignore and agent-doc writes; `bin/tusk-upgrade.py` reads it to decide which steps to run during an upgrade. Legacy plain-form markers (`claude` / `codex`) written by older installs are accepted and treated as `<mode>-source`.

## Source-vs-consumer role

In addition to mode, install.sh detects a **role** — whether the install target is the tusk source repo itself or a downstream consumer project — by comparing `$SCRIPT_DIR` (the install.sh location) against `$REPO_ROOT` (the install target). When they match, the role is `source`; otherwise `consumer`.

Two Claude hooks path-filter on the tusk source layout (`skills/*`, `bin/*`, `config.default.json`, `install.sh`) and would silently no-op on every invocation in a consumer install. They are skipped from `.claude/settings.json` and from the `pre-push` git dispatcher in consumer mode rather than registered as dead code:

| Hook | Event | Source role | Consumer role |
| --- | --- | --- | --- |
| `setup-path.sh` | SessionStart | registered | registered |
| `inject-task-context.sh` | SessionStart | registered | registered |
| `block-raw-sqlite.sh` | PreToolUse(Bash) | registered | registered |
| `dupe-gate.sh` | PreToolUse(Bash) | registered | registered |
| `block-sql-neq.sh` | PreToolUse(Bash) | registered | registered |
| `commit-msg-format.sh` | PreToolUse(Bash) | registered | registered |
| `branch-naming.sh` | PreToolUse(Bash) | registered | registered |
| `version-bump-check.sh` | PreToolUse(Bash) | registered | **skipped** |
| `conventions-preflight.sh` | PreToolUse(Edit\|Write) | registered | registered |
| `auto-lint.sh` | PostToolUse(Edit\|Write) | registered | **skipped** |
| `check-criteria-on-stop.sh` | Stop | registered | registered |

The `pre-push` git-event dispatcher is similarly trimmed: it runs `branch-naming` in both roles, and `version-bump-check` only in source-role installs. The `pre-commit` and `commit-msg` dispatchers are unchanged across roles.

The hook *files* are still copied to `.claude/hooks/` in both roles — only the settings.json registration and the dispatcher wiring are skipped. This keeps a future role transition cheap: a reinstall just rewrites settings.json. Source-only hooks logged as `Skipped source-only hook (consumer install): <path>` during install make the choice auditable.

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

## Model routing for read-only prompts

Codex prompt files have no `model:` frontmatter — the Codex CLI resolves the model from `~/.codex/config.toml`, named profiles activated via `--config-profile`, or the per-invocation `--model` flag. The Claude side of tusk pins read-only/orchestration skills (`groom-backlog`, `tusk-insights`, `tusk-update`, `loop`) to a cheaper model via SKILL.md frontmatter; Codex users can get the same effect with a named profile.

Add a profile to `~/.codex/config.toml`:

```toml
[profiles.cheap]
model = "gpt-4.1-mini"
```

Then invoke the four prompts above with `--config-profile cheap`:

```bash
codex --config-profile cheap exec /loop
codex --config-profile cheap exec /groom-backlog
codex --config-profile cheap exec /tusk-insights
codex --config-profile cheap exec /tusk-update
```

Leave the other prompts (`/tusk`, `/chain`, `/investigate`, `/create-task`, `/retro`, `/review-commits`, `/address-issue`, `/resume-task`, `/tusk-init`, `/investigate-directory`) on your default (top-tier) model — they do real reasoning and benefit from the bigger model. Choose any model name your Codex install supports; `gpt-4.1-mini` is the cheap-tier default suggestion, swap as needed.

## Feature parity

Every Claude skill has a corresponding Codex prompt under [`.codex/prompts/`](../codex-prompts/) (sourced from `codex-prompts/` in this repo and copied to `<repo_root>/.codex/prompts/` on install). The mechanical pipelines that previously lived inline in skill bodies have been migrated into `tusk` CLI orchestrators where it made sense — `tusk groom`, `tusk retro`, `tusk init-wizard`, `tusk loop`, and the `tusk review …` family — so the Codex prompts can drive the same logic the Claude skills do, plus the interactive layer.

| Claude skill              | Codex equivalent                                       | Notes |
| ------------------------- | ------------------------------------------------------ | ----- |
| `/tusk`                   | `.codex/prompts/tusk.md`                               | Sequential — no parallel sub-agents |
| `/create-task`            | `.codex/prompts/create-task.md`                        |       |
| `/groom-backlog`          | `tusk groom` + `.codex/prompts/groom-backlog.md`       | CLI runs autoclose + backlog-scan + lint; prompt adds analysis |
| `/retro`                  | `tusk retro` + `.codex/prompts/retro.md`               | CLI emits per-task signals + cross-retro themes |
| `/chain`                  | `.codex/prompts/chain.md`                              | Sequential — one task at a time, no parallel waves |
| `/loop`                   | `tusk loop` + `.codex/prompts/loop.md`                 | Sequential — one task at a time |
| `/review-commits`         | `tusk review …` + `.codex/prompts/review-commits.md`   | Sequential — runs inline (no Task tool / no parallel reviewer agent) |
| `/tusk-init`              | `tusk init-wizard` + `.codex/prompts/tusk-init.md`     | CLI is canonical entry point; prompt orchestrates seeding |
| `/tusk-update`            | `.codex/prompts/tusk-update.md`                        |       |
| `/tusk-insights`          | `.codex/prompts/tusk-insights.md`                      |       |
| `/investigate`            | `.codex/prompts/investigate.md`                        |       |
| `/investigate-directory`  | `.codex/prompts/investigate-directory.md`              |       |
| `/resume-task`            | `.codex/prompts/resume-task.md`                        |       |
| `/address-issue`          | `.codex/prompts/address-issue.md`                      |       |

The Codex-only gaps that remain:

- The PreToolUse/PostToolUse hooks (e.g. `conventions-preflight.sh`) that fire on tool invocations — Codex has no hook primitive.
- The permissions allowlist entries required for Claude's `/review-commits` parallel-reviewer flow — Codex has no Task tool, so its review runs inline in the active session (see `.codex/prompts/review-commits.md` for the tradeoff note).

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
