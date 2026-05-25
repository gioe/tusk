#!/usr/bin/env bash
#
# Install tusker into a Claude Code or Codex project.
#
# Usage:
#   cd /path/to/your/project
#   /path/to/tusker/install.sh
#
# Agent-mode detection (auto):
#   - .claude/ directory present → Claude Code mode; install to .claude/bin/,
#     copy skills/hooks, and merge .claude/settings.json.
#   - AGENTS.md present → Codex mode; install to tusk/bin/ and copy
#     codex-prompts/*.md to .codex/prompts/.
#   - Both markers present → dual mode; install both Claude and Codex surfaces.
#   - Neither → error out with a helpful message.
#
# What it does:
#   1. Copies bin/tusk + Python scripts → <install_dir>/
#   2. Copies config, VERSION, pricing  → <install_dir>/
#   3. Claude mode only: copies skills/* → .claude/skills/*
#   4. Claude mode only: copies .claude/hooks/ scripts + merges hooks
#      and permissions.allow into .claude/settings.json
#   5. Writes <install_dir>/install-mode marker so upgrades know which mode to apply
#   6. Runs tusk init + migrate
#   7. Prints next steps

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve REPO_ROOT — prefer the git toplevel when one exists, otherwise fall
# back to $PWD so install.sh can run in fresh, not-yet-initialised projects.
# /tusk-init's fresh-project flow prompts for `git init` after install.
if REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  :
else
  REPO_ROOT="$PWD"
fi

# ── Agent-mode detection ─────────────────────────────────────────────
HAS_CLAUDE=0
HAS_CODEX=0
[[ -d "$REPO_ROOT/.claude" ]] && HAS_CLAUDE=1
[[ -f "$REPO_ROOT/AGENTS.md" ]] && HAS_CODEX=1

if [[ "$HAS_CLAUDE" -eq 1 && "$HAS_CODEX" -eq 1 ]]; then
  INSTALL_MODE="dual"
  AGENT_DIR=".claude"
  INSTALL_DIR=".claude/bin"
  MANIFEST_PATH=".claude/tusk-manifest.json"
elif [[ "$HAS_CLAUDE" -eq 1 ]]; then
  INSTALL_MODE="claude"
  AGENT_DIR=".claude"
  INSTALL_DIR=".claude/bin"
  MANIFEST_PATH=".claude/tusk-manifest.json"
elif [[ "$HAS_CODEX" -eq 1 ]]; then
  INSTALL_MODE="codex"
  AGENT_DIR="tusk"
  INSTALL_DIR="tusk/bin"
  MANIFEST_PATH="tusk/tusk-manifest.json"
else
  echo "Error: No .claude/ directory or AGENTS.md found at $REPO_ROOT." >&2
  echo "       Initialize Claude Code (creates .claude/) or Codex (creates AGENTS.md) first." >&2
  exit 1
fi

# ── Role detection: tusk source repo vs consumer install ────────────
# install.sh runs from inside the tusk source repo (developer workflow) when
# SCRIPT_DIR equals REPO_ROOT. Otherwise install.sh was extracted from a
# tarball or cloned alongside a downstream project — i.e., a consumer install.
# Source-only hooks (auto-lint.sh, version-bump-check.sh) filter on paths that
# only exist in the source repo, so they're skipped from settings.json and the
# pre-push dispatcher in consumer mode rather than silently no-opping forever.
if [[ "$SCRIPT_DIR" == "$REPO_ROOT" ]]; then
  INSTALL_ROLE="source"
else
  INSTALL_ROLE="consumer"
fi

# Hooks whose path filters target the tusk source layout (skills/*, bin/*) and
# are silent no-ops in any other repo. Skipped from settings.json registration
# and pre-push dispatcher when INSTALL_ROLE=consumer.
SOURCE_ONLY_HOOK_BASENAMES="auto-lint.sh version-bump-check.sh"

echo "Installing tusker into $REPO_ROOT (mode: $INSTALL_MODE, role: $INSTALL_ROLE, install dir: $INSTALL_DIR)"

# ── 1b. Resolve project_type before the skill-filter loop ───────────
# Issue #878: the project_type-gated skill-filter loop in section 3 ran
# before any manifest detection, so a fresh ios_app install (Package.swift
# present, no tusk/config.json yet) silently skipped /ios-libs-issue and
# /ios-libs-contribute. Compute project_type here — existing config value
# wins over detection, mirroring the post-init block's no-clobber guard.
# The post-init block at section 5b still writes the detected value into
# tusk/config.json via 'tusk init-write-config'; this hoist only changes
# which value section 3's skill filter sees.
EXISTING_PROJECT_TYPE_EARLY="$(python3 -c "
import json, os
p = os.path.join('$REPO_ROOT', 'tusk', 'config.json')
if os.path.isfile(p):
    try:
        v = json.load(open(p, encoding='utf-8')).get('project_type')
        print(v if v is not None else '')
    except Exception:
        print('')
")"
DETECTED_PROJECT_TYPE="$(python3 -c "
import glob, os
root = '$REPO_ROOT'
if (os.path.isfile(os.path.join(root, 'Package.swift'))
    or glob.glob(os.path.join(root, '*.xcodeproj'))
    or glob.glob(os.path.join(root, '*.xcworkspace'))):
    print('ios_app')
elif (os.path.isfile(os.path.join(root, 'pyproject.toml'))
      or os.path.isfile(os.path.join(root, 'setup.py'))
      or os.path.isfile(os.path.join(root, 'requirements.txt'))):
    print('python_service')
elif os.path.isfile(os.path.join(root, 'package.json')):
    print('web_app')
else:
    print('')
")"
if [[ -n "$EXISTING_PROJECT_TYPE_EARLY" ]]; then
  PROJECT_TYPE="$EXISTING_PROJECT_TYPE_EARLY"
else
  PROJECT_TYPE="$DETECTED_PROJECT_TYPE"
fi

# ── 1. Copy bin + support files ──────────────────────────────────────
mkdir -p "$REPO_ROOT/$INSTALL_DIR"
cp "$SCRIPT_DIR/bin/tusk" "$REPO_ROOT/$INSTALL_DIR/tusk"
chmod +x "$REPO_ROOT/$INSTALL_DIR/tusk"
echo "  Installed $INSTALL_DIR/tusk"

# Scripts that are only meaningful in the tusk source repo — not distributed.
# Canonical source: bin/dist-excluded.txt (also read by tusk-generate-manifest.py and tusk-lint.py).
TUSK_SKIP_SCRIPTS=$(tr '\n' ' ' < "$SCRIPT_DIR/bin/dist-excluded.txt")

# Copy Python scripts alongside binary (needed for $SCRIPT_DIR dispatch)
for pyfile in "$SCRIPT_DIR"/bin/tusk-*.py; do
  [[ -f "$pyfile" ]] || continue
  basename_py="$(basename "$pyfile")"
  if [[ " $TUSK_SKIP_SCRIPTS " == *" $basename_py "* ]]; then
    continue
  fi
  cp "$pyfile" "$REPO_ROOT/$INSTALL_DIR/"
  echo "  Installed $INSTALL_DIR/$basename_py"
done

# Record the baseline hash of tusk-lint.py so upgrades can detect true local modifications.
python3 -c "import hashlib, pathlib; p = pathlib.Path('$REPO_ROOT/$INSTALL_DIR/tusk-lint.py'); pathlib.Path('$REPO_ROOT/$INSTALL_DIR/tusk-lint.py.hash').write_text(hashlib.md5(p.read_bytes()).hexdigest() + '\n')"
echo "  Recorded $INSTALL_DIR/tusk-lint.py.hash"

# tusk_loader.py uses an underscore filename (importable without importlib) — copy explicitly.
cp "$SCRIPT_DIR/bin/tusk_loader.py" "$REPO_ROOT/$INSTALL_DIR/tusk_loader.py"
echo "  Installed $INSTALL_DIR/tusk_loader.py"

# tusk_skill_filter.py — same underscore-filename pattern as tusk_loader.py.
# Provides applies_to_project_types gating used by install.sh below and tusk-upgrade.py.
cp "$SCRIPT_DIR/bin/tusk_skill_filter.py" "$REPO_ROOT/$INSTALL_DIR/tusk_skill_filter.py"
echo "  Installed $INSTALL_DIR/tusk_skill_filter.py"

# tusk_github.py — same underscore-filename pattern; shared GitHub-fetch helpers
# used by tusk-upgrade.py and tusk-reconcile-skills.py.
cp "$SCRIPT_DIR/bin/tusk_github.py" "$REPO_ROOT/$INSTALL_DIR/tusk_github.py"
echo "  Installed $INSTALL_DIR/tusk_github.py"

# tusk_underscore_bin_files.py — canonical list of underscore-named bin/ files
# (consumed by tusk-upgrade.py, tusk-lint.py, and tusk-generate-manifest.py).
cp "$SCRIPT_DIR/bin/tusk_underscore_bin_files.py" "$REPO_ROOT/$INSTALL_DIR/tusk_underscore_bin_files.py"
echo "  Installed $INSTALL_DIR/tusk_underscore_bin_files.py"

# ── 2. Copy config, VERSION ─────────────────────────────────────────
cp "$SCRIPT_DIR/config.default.json" "$REPO_ROOT/$INSTALL_DIR/config.default.json"
echo "  Installed $INSTALL_DIR/config.default.json"

cp "$SCRIPT_DIR/VERSION" "$REPO_ROOT/$INSTALL_DIR/VERSION"
echo "  Installed $INSTALL_DIR/VERSION"

cp "$SCRIPT_DIR/pricing.json" "$REPO_ROOT/$INSTALL_DIR/pricing.json"
echo "  Installed $INSTALL_DIR/pricing.json"

# Stamp install-mode marker so tusk-upgrade.py can apply the right mode-specific
# logic. Compound form "<mode>-<role>" lets readers distinguish a tusk-source
# install from a consumer install. Legacy plain "claude" / "codex" markers
# (pre-role) are treated as source for backward compatibility by readers.
echo "${INSTALL_MODE}-${INSTALL_ROLE}" > "$REPO_ROOT/$INSTALL_DIR/install-mode"
echo "  Stamped $INSTALL_DIR/install-mode (${INSTALL_MODE}-${INSTALL_ROLE})"

# ── 3. Copy skills (claude mode) or codex prompts (codex mode) ───────
# Skills that declare `applies_to_project_types` in their SKILL.md frontmatter
# install only when the target's project_type matches one of the listed types.
# Universal skills (no field) always install. PROJECT_TYPE was resolved in
# section 1b above — existing tusk/config.json wins, otherwise the manifest
# detection result is used. On fresh installs without a manifest signal,
# PROJECT_TYPE is empty and gated skills are still deferred (typical
# pre-/tusk-init state).
if [[ "$INSTALL_MODE" == "claude" || "$INSTALL_MODE" == "dual" ]]; then
  for skill_dir in "$SCRIPT_DIR"/skills/*/; do
    skill_name="$(basename "$skill_dir")"
    if ! python3 "$SCRIPT_DIR/bin/tusk_skill_filter.py" --skill "$skill_dir" --project-type "$PROJECT_TYPE"; then
      echo "  Skipped skill (project_type-gated): $skill_name"
      continue
    fi
    mkdir -p "$REPO_ROOT/.claude/skills/$skill_name"
    cp "$skill_dir"* "$REPO_ROOT/.claude/skills/$skill_name/" 2>/dev/null || true
    echo "  Installed skill: $skill_name"
  done
else
  echo "  Skipping skills (Codex mode has no skills primitive)"
fi

if [[ "$INSTALL_MODE" == "codex" || "$INSTALL_MODE" == "dual" ]]; then
  if [[ -d "$SCRIPT_DIR/codex-prompts" ]]; then
    mkdir -p "$REPO_ROOT/.codex/prompts"
    for prompt_file in "$SCRIPT_DIR"/codex-prompts/*.md; do
      [[ -f "$prompt_file" ]] || continue
      prompt_name="$(basename "$prompt_file")"
      cp "$prompt_file" "$REPO_ROOT/.codex/prompts/$prompt_name"
      echo "  Installed .codex/prompts/$prompt_name"
    done
  fi
fi

# ── 4. Copy hooks + merge settings (claude mode only) ────────────────
if [[ "$INSTALL_MODE" == "claude" || "$INSTALL_MODE" == "dual" ]]; then
  mkdir -p "$REPO_ROOT/.claude/hooks"

  # Copy all hook scripts from the tusk source repo
  for hookfile in "$SCRIPT_DIR"/.claude/hooks/*; do
    [[ -f "$hookfile" ]] || continue
    hookname="$(basename "$hookfile")"
    if [[ -e "$REPO_ROOT/.claude/hooks/$hookname" && "$hookfile" -ef "$REPO_ROOT/.claude/hooks/$hookname" ]]; then
      echo "  Hook already in place: .claude/hooks/$hookname"
      continue
    fi
    cp "$hookfile" "$REPO_ROOT/.claude/hooks/$hookname"
    chmod +x "$REPO_ROOT/.claude/hooks/$hookname"
    echo "  Installed .claude/hooks/$hookname"
  done

  # Override setup-path.sh for target projects — source version adds bin/ to PATH,
  # but installed projects need .claude/bin/ on PATH instead.
  if [[ "$INSTALL_ROLE" == "consumer" ]]; then
    cat > "$REPO_ROOT/.claude/hooks/setup-path.sh" << 'HOOKEOF'
#!/bin/bash
# Added by tusk install — puts .claude/bin on PATH for Claude Code sessions
if [ -n "$CLAUDE_ENV_FILE" ]; then
  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  echo "export PATH=\"$REPO_ROOT/.claude/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi
exit 0
HOOKEOF
    chmod +x "$REPO_ROOT/.claude/hooks/setup-path.sh"
  fi

  # ── 4b. Merge hooks and permissions.allow into .claude/settings.json ─
  python3 -c "
import json, os

source_settings_path = os.path.join('$SCRIPT_DIR', '.claude', 'settings.json')
target_settings_path = os.path.join('$REPO_ROOT', '.claude', 'settings.json')
install_role = '$INSTALL_ROLE'
source_only_hooks = set(filter(None, '$SOURCE_ONLY_HOOK_BASENAMES'.split()))

# Skip merge if source settings.json is absent
if not os.path.isfile(source_settings_path):
    print('  Warning: source settings.json not found, skipping hooks and permissions merge')
    exit(0)

# Read source settings once
with open(source_settings_path) as f:
    source_settings = json.load(f)

source_hooks = source_settings.get('hooks', {})
source_allow = source_settings.get('permissions', {}).get('allow', [])

# Read existing target settings (or start fresh)
if os.path.exists(target_settings_path):
    with open(target_settings_path) as f:
        target_settings = json.load(f)
else:
    target_settings = {}


def _is_source_only(group):
    # Filter hook groups whose command basename matches a source-only script.
    # Hooks like auto-lint.sh and version-bump-check.sh path-filter on skills/
    # and bin/, which only exist in tusk's source repo — registering them in a
    # consumer is dead weight that silently exits 0 on every invocation.
    for h in group.get('hooks', []):
        cmd = h.get('command', '')
        if not cmd:
            continue
        if os.path.basename(cmd) in source_only_hooks:
            return True
    return False


# Merge hook registrations
target_hooks = target_settings.setdefault('hooks', {})
for event_type, source_groups in source_hooks.items():
    target_groups = target_hooks.setdefault(event_type, [])

    # Collect commands already registered in target
    existing_commands = set()
    for group in target_groups:
        for h in group.get('hooks', []):
            cmd = h.get('command', '')
            if cmd:
                existing_commands.add(cmd)

    # Add missing hook groups from source
    for group in source_groups:
        group_commands = [h.get('command', '') for h in group.get('hooks', [])]
        if install_role == 'consumer' and _is_source_only(group):
            for cmd in group_commands:
                if cmd:
                    print(f'  Skipped source-only hook (consumer install): {cmd}')
            continue
        if not any(cmd in existing_commands for cmd in group_commands if cmd):
            target_groups.append(group)
            for cmd in group_commands:
                if cmd:
                    print(f'  Registered hook: {cmd}')
        else:
            for cmd in group_commands:
                if cmd:
                    print(f'  Hook already registered: {cmd}')

# Merge permissions.allow entries
target_allow = target_settings.setdefault('permissions', {}).setdefault('allow', [])
existing = set(target_allow)
for entry in source_allow:
    if entry not in existing:
        target_allow.append(entry)
        existing.add(entry)
        print(f'  Added permission: {entry}')
    else:
        print(f'  Permission already present: {entry}')

# Ensure review-commits required entries are present even if source settings.json
# omits them or is absent. Keeps install.sh in sync with tusk-upgrade.py's
# ensure_review_commits_permissions() — keep both lists aligned when editing.
required_review_perms = [
    'Bash(git diff:*)',
    'Bash(git remote:*)',
    'Bash(git symbolic-ref:*)',
    'Bash(git branch:*)',
    'Bash(tusk review:*)',
]
for entry in required_review_perms:
    if entry not in existing:
        target_allow.append(entry)
        existing.add(entry)
        print(f'  Added required permission: {entry}')

# Write target settings once
with open(target_settings_path, 'w') as f:
    json.dump(target_settings, f, indent=2)
    f.write('\n')
"
else
  echo "  Skipping hooks and settings.json merge (Codex mode has no hooks primitive)"
fi

# ── 4d. Install git-event guards + dispatchers (both modes) ──────────
# Copy hooks/git/*.sh guard scripts into <install_dir>/hooks/git/
GIT_GUARDS_SRC="$SCRIPT_DIR/hooks/git"
GIT_GUARDS_DST="$REPO_ROOT/$INSTALL_DIR/hooks/git"
mkdir -p "$GIT_GUARDS_DST"
for guard in "$GIT_GUARDS_SRC"/*.sh; do
  [[ -f "$guard" ]] || continue
  guardname="$(basename "$guard")"
  cp "$guard" "$GIT_GUARDS_DST/$guardname"
  chmod +x "$GIT_GUARDS_DST/$guardname"
  echo "  Installed $INSTALL_DIR/hooks/git/$guardname"
done

# Write .git/hooks/{pre-commit,pre-push,commit-msg} dispatchers. The dispatchers
# carry a TUSK_HOOK_DISPATCHER_V1 marker so re-runs stay idempotent. When an
# existing non-tusk hook is present at any of those paths, it is renamed to
# <event>.pre-tusk (once) and invoked from the dispatcher so external hooks are
# preserved rather than overwritten.
GIT_HOOKS_DIR="$REPO_ROOT/.git/hooks"
if [[ -d "$GIT_HOOKS_DIR" ]]; then
  TUSK_HOOK_MARKER="TUSK_HOOK_DISPATCHER_V1"

  write_dispatcher() {
    local event="$1"; shift
    local guards="$*"
    local target="$GIT_HOOKS_DIR/$event"
    local chained="$target.pre-tusk"
    local chained_present=0

    if [[ -e "$target" ]] && ! grep -q "$TUSK_HOOK_MARKER" "$target" 2>/dev/null; then
      if [[ ! -e "$chained" ]]; then
        mv "$target" "$chained"
        chmod +x "$chained"
      fi
    fi
    [[ -e "$chained" ]] && chained_present=1

    {
      printf '#!/bin/bash\n'
      printf '# %s — managed by tusk install.sh; do not edit.\n' "$TUSK_HOOK_MARKER"
      printf '\n'
      printf 'HERE="$(cd "$(dirname "$0")" && pwd)"\n'
      printf 'REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"\n'
      printf '\n'
      printf 'HOOKS_DIR=""\n'
      printf 'if [[ -d "$REPO_ROOT/.claude/bin/hooks/git" ]]; then\n'
      printf '  HOOKS_DIR="$REPO_ROOT/.claude/bin/hooks/git"\n'
      printf 'elif [[ -d "$REPO_ROOT/tusk/bin/hooks/git" ]]; then\n'
      printf '  HOOKS_DIR="$REPO_ROOT/tusk/bin/hooks/git"\n'
      printf 'fi\n'
      printf '\n'
      printf 'if [[ -n "$HOOKS_DIR" ]]; then\n'
      printf '  for g in %s; do\n' "$guards"
      printf '    script="$HOOKS_DIR/$g.sh"\n'
      printf '    [[ -x "$script" ]] || continue\n'
      printf '    "$script" "$@"\n'
      printf '    rc=$?\n'
      printf '    [[ $rc -eq 0 ]] || exit $rc\n'
      printf '  done\n'
      printf 'fi\n'
      printf '\n'
      printf 'if [[ -x "$HERE/%s.pre-tusk" ]]; then\n' "$event"
      printf '  exec "$HERE/%s.pre-tusk" "$@"\n' "$event"
      printf 'fi\n'
      printf '\n'
      printf 'exit 0\n'
    } > "$target"
    chmod +x "$target"

    if [[ "$chained_present" -eq 1 ]]; then
      echo "  Installed .git/hooks/$event (chains existing .git/hooks/$event.pre-tusk)"
    else
      echo "  Installed .git/hooks/$event"
    fi
  }

  # version-bump-check guards changes to bin/, skills/, config.default.json,
  # and install.sh — paths that only exist in the tusk source repo. In a
  # consumer install it is a silent no-op on every push, so omit it from the
  # dispatcher rather than wire up dead code.
  if [[ "$INSTALL_ROLE" == "consumer" ]]; then
    pre_push_guards="branch-naming"
  else
    pre_push_guards="branch-naming version-bump-check"
  fi
  write_dispatcher pre-commit "block-raw-sqlite block-sql-neq dupe-gate"
  write_dispatcher pre-push   "$pre_push_guards"
  write_dispatcher commit-msg "commit-msg-format"
else
  echo "  Warning: $REPO_ROOT/.git/hooks/ not found — skipping git-event dispatcher install"
fi

if [[ "$INSTALL_MODE" == "dual" && "$INSTALL_ROLE" == "consumer" ]]; then
  mkdir -p "$REPO_ROOT/tusk/bin"
  cp -R "$REPO_ROOT/.claude/bin/." "$REPO_ROOT/tusk/bin/"
  echo "${INSTALL_MODE}-${INSTALL_ROLE}" > "$REPO_ROOT/tusk/bin/install-mode"
  echo "  Installed tusk/bin/ for Codex surface"
fi

# ── 4c. Write tusk-manifest.json ─────────────────────────────────────
python3 -c "
import json, os, glob

script_dir = '$SCRIPT_DIR'
repo_root = '$REPO_ROOT'
install_mode = '$INSTALL_MODE'
install_role = '$INSTALL_ROLE'
install_dir = '$INSTALL_DIR'
manifest_path_rel = '$MANIFEST_PATH'

files = []
install_dirs = [install_dir]
if install_mode == 'dual' and install_role == 'consumer':
    install_dirs = ['.claude/bin', 'tusk/bin']

# Canonical source: bin/dist-excluded.txt (also read by TUSK_SKIP_SCRIPTS above and tusk-lint.py).
with open(os.path.join(script_dir, 'bin', 'dist-excluded.txt'), encoding='utf-8') as _f:
    dist_excluded = {line.strip() for line in _f if line.strip()}

for _install_dir in install_dirs:
    files.append(_install_dir + '/tusk')
for p in sorted(glob.glob(os.path.join(script_dir, 'bin', 'tusk-*.py'))):
    if os.path.basename(p) in dist_excluded:
        continue
    for _install_dir in install_dirs:
        files.append(_install_dir + '/' + os.path.basename(p))

# Underscore-named bin/ files — canonical list lives in bin/tusk_underscore_bin_files.py.
import sys as _sys
_sys.path.insert(0, os.path.join(script_dir, 'bin'))
import tusk_underscore_bin_files as _ubf
_sys.path.pop(0)
for _name in _ubf.get_underscore_bin_files(script_dir):
    for _install_dir in install_dirs:
        files.append(_install_dir + '/' + _name)

for name in ['config.default.json', 'VERSION', 'pricing.json']:
    for _install_dir in install_dirs:
        files.append(_install_dir + '/' + name)

# Skills/hooks exist when Claude is present; codex prompts exist when Codex is present.
if install_mode in ('claude', 'dual'):
    # Filter skills by applies_to_project_types — see bin/tusk_skill_filter.py.
    import sys as _sys
    _sys.path.insert(0, os.path.join(script_dir, 'bin'))
    import tusk_skill_filter as _sf
    _sys.path.pop(0)
    _project_type = _sf.get_project_type(repo_root)
    for skill_dir in sorted(glob.glob(os.path.join(script_dir, 'skills', '*/'))):
        skill_name = os.path.basename(skill_dir.rstrip('/'))
        if install_role != 'source' and not _sf.should_install_skill(skill_dir, _project_type):
            continue
        for fname in sorted(os.listdir(skill_dir)):
            full = os.path.join(skill_dir, fname)
            if os.path.isfile(full):
                files.append('.claude/skills/' + skill_name + '/' + fname)

    hooks_src = os.path.join(script_dir, '.claude', 'hooks')
    if os.path.isdir(hooks_src):
        for fname in sorted(os.listdir(hooks_src)):
            full = os.path.join(hooks_src, fname)
            if os.path.isfile(full):
                files.append('.claude/hooks/' + fname)
if install_mode in ('codex', 'dual'):
    prompts_src = os.path.join(script_dir, 'codex-prompts')
    if os.path.isdir(prompts_src):
        for fname in sorted(os.listdir(prompts_src)):
            full = os.path.join(prompts_src, fname)
            if os.path.isfile(full) and fname.endswith('.md'):
                files.append('.codex/prompts/' + fname)

# hooks/git/*.sh ships in both install modes
git_hooks_src = os.path.join(script_dir, 'hooks', 'git')
if os.path.isdir(git_hooks_src):
    for fname in sorted(os.listdir(git_hooks_src)):
        full = os.path.join(git_hooks_src, fname)
        if os.path.isfile(full):
            for _install_dir in install_dirs:
                files.append(_install_dir + '/hooks/git/' + fname)

manifest_paths = [manifest_path_rel]
if install_mode == 'dual' and install_role == 'consumer':
    manifest_paths = ['.claude/tusk-manifest.json', 'tusk/tusk-manifest.json']
for _manifest_path_rel in manifest_paths:
    manifest_full = os.path.join(repo_root, _manifest_path_rel)
    os.makedirs(os.path.dirname(manifest_full), exist_ok=True)
    with open(manifest_full, 'w') as f:
        json.dump(files, f, indent=2)
        f.write('\n')
    print('  Wrote ' + _manifest_path_rel + ' (' + str(len(files)) + ' entries)')
"

# ── 5. Init database + migrate ───────────────────────────────────────
TUSK="$REPO_ROOT/$INSTALL_DIR/tusk"
"$TUSK" init
"$TUSK" migrate

# ── 5b. Auto-detect project_type from manifest files ────────────────
# Issue #854 follow-up: TASK-446 added a runtime canonical-fallback in
# 'task-worktree create' for projects whose worktree.symlink_files is empty,
# but install.sh-only installs still ship with an empty list in
# tusk/config.json. This block closes that gap by detecting project_type
# from manifest signatures and invoking 'tusk init-write-config --project-type
# <detected>' so the existing WORKTREE_SYMLINK_DEFAULTS auto-seed in
# bin/tusk-init-write-config.py populates worktree.symlink_files explicitly.
#
# Detection order (most-specific signal first; first match wins):
#   1. ios_app:        Package.swift OR *.xcodeproj OR *.xcworkspace
#   2. python_service: pyproject.toml OR setup.py OR requirements.txt
#   3. web_app:        package.json
#
# When no signals match, behavior is identical to pre-task install.sh —
# project_type stays at the default null and the TASK-446 runtime fallback
# handles the gap when a worktree is created. When project_type is already
# set in tusk/config.json (e.g. install.sh re-run after /tusk-init), this
# block is a no-op so user customization is never overwritten.
EXISTING_PROJECT_TYPE="$(python3 -c "
import json, os
p = os.path.join('$REPO_ROOT', 'tusk', 'config.json')
if os.path.isfile(p):
    try:
        v = json.load(open(p, encoding='utf-8')).get('project_type')
        print(v if v is not None else '')
    except Exception:
        print('')
")"
if [[ -z "$EXISTING_PROJECT_TYPE" ]]; then
  DETECTED_TYPE="$(python3 -c "
import glob, os
root = '$REPO_ROOT'
if (os.path.isfile(os.path.join(root, 'Package.swift'))
    or glob.glob(os.path.join(root, '*.xcodeproj'))
    or glob.glob(os.path.join(root, '*.xcworkspace'))):
    print('ios_app')
elif (os.path.isfile(os.path.join(root, 'pyproject.toml'))
      or os.path.isfile(os.path.join(root, 'setup.py'))
      or os.path.isfile(os.path.join(root, 'requirements.txt'))):
    print('python_service')
elif os.path.isfile(os.path.join(root, 'package.json')):
    print('web_app')
else:
    print('')
")"
  if [[ -n "$DETECTED_TYPE" ]]; then
    echo "  Detected project_type: $DETECTED_TYPE — seeding tusk/config.json via init-write-config"
    "$TUSK" init-write-config --project-type "$DETECTED_TYPE" > /dev/null
  fi
fi

# ── 6. Print next steps ───────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  Installation complete! (mode: $INSTALL_MODE)"
echo "════════════════════════════════════════════════════════════════"
echo ""
if [[ "$INSTALL_MODE" == "claude" || "$INSTALL_MODE" == "dual" ]]; then
  if [[ "$INSTALL_MODE" == "dual" ]]; then
    echo "Next steps (Claude Code):"
  else
    echo "Next steps:"
  fi
  echo ""
  echo "  1. Start a NEW Claude Code session (skills are discovered at startup,"
  echo "     so /tusk-init won't be available in the session that ran install.sh)"
  echo ""
  echo "  2. Run /tusk-init to configure your project interactively"
  echo "     (sets domains, agents, CLAUDE.md snippet, and seeds tasks from TODOs)"
  echo ""
  echo "  Or configure manually:"
  echo "     a. Edit tusk/config.json to set your project's domains and agents"
  echo "     b. Run: tusk init --force"
  echo "     c. Add the Task Queue snippet to your CLAUDE.md (see /tusk-init)"
fi

if [[ "$INSTALL_MODE" == "codex" || "$INSTALL_MODE" == "dual" ]]; then
  if [[ "$INSTALL_MODE" == "dual" ]]; then
    echo ""
    echo "Next steps (Codex):"
  else
    echo "Next steps (Codex mode):"
  fi
  echo ""
  if command -v direnv >/dev/null 2>&1 && [[ ! -e "$REPO_ROOT/.envrc" ]]; then
    echo "PATH_add tusk/bin" > "$REPO_ROOT/.envrc"
    echo "  1. Wrote .envrc with 'PATH_add tusk/bin'. Run 'direnv allow' so 'tusk' is invocable:"
    echo "       direnv allow"
  else
    echo "  1. Add tusk/bin to your PATH so 'tusk' is invocable:"
    echo "       export PATH=\"$REPO_ROOT/tusk/bin:\$PATH\""
  fi
  echo ""
  echo "  2. Edit tusk/config.json to set your project's domains and agents."
  echo ""
  echo "  3. Tusk appended task-tool guidance to AGENTS.md (same block as CLAUDE.md"
  echo "     in Claude mode). Review it so Codex knows to use the tusk CLI."
  if [[ "$INSTALL_MODE" == "codex" ]]; then
    echo ""
    echo "  Note: Claude-specific features (skills, hooks, settings.json) are not"
    echo "  installed in Codex mode. See docs/CODEX.md for details."
  fi
fi
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  Found a bug? https://github.com/gioe/tusk/issues"
echo "════════════════════════════════════════════════════════════════"
echo ""
