#!/usr/bin/env bash
#
# Install tusker into a Claude Code project.
#
# Usage:
#   cd /path/to/your/project
#   /path/to/tusker/install.sh
#
# What it does:
#   1. Copies bin/tusk + Python scripts → .claude/bin/
#   2. Copies config, VERSION, pricing  → .claude/bin/
#   3. Copies skills/*                  → .claude/skills/*
#   4. Copies .claude/hooks/ scripts + merges registrations into settings.json
#   5. Runs tusk init + migrate
#   6. Prints next steps

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Must be run from a git repo root
if ! git rev-parse --show-toplevel &>/dev/null; then
  echo "Error: Run this from a git repository root." >&2
  exit 1
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"

# Must have Claude Code initialized
if [[ ! -d "$REPO_ROOT/.claude" ]]; then
  echo "Error: No .claude/ directory found. Initialize Claude Code first." >&2
  exit 1
fi

echo "Installing tusker into $REPO_ROOT"

# ── 1. Copy bin + support files ──────────────────────────────────────
mkdir -p "$REPO_ROOT/.claude/bin"
cp "$SCRIPT_DIR/bin/tusk" "$REPO_ROOT/.claude/bin/tusk"
chmod +x "$REPO_ROOT/.claude/bin/tusk"
echo "  Installed .claude/bin/tusk"

# Copy Python scripts alongside binary (needed for $SCRIPT_DIR dispatch)
for pyfile in "$SCRIPT_DIR"/bin/tusk-*.py; do
  [[ -f "$pyfile" ]] || continue
  cp "$pyfile" "$REPO_ROOT/.claude/bin/"
  echo "  Installed .claude/bin/$(basename "$pyfile")"
done

# ── 2. Copy config, VERSION ─────────────────────────────────────────
cp "$SCRIPT_DIR/config.default.json" "$REPO_ROOT/.claude/bin/config.default.json"
echo "  Installed .claude/bin/config.default.json"

cp "$SCRIPT_DIR/VERSION" "$REPO_ROOT/.claude/bin/VERSION"
echo "  Installed .claude/bin/VERSION"

cp "$SCRIPT_DIR/pricing.json" "$REPO_ROOT/.claude/bin/pricing.json"
echo "  Installed .claude/bin/pricing.json"

# ── 3. Copy skills ───────────────────────────────────────────────────
for skill_dir in "$SCRIPT_DIR"/skills/*/; do
  skill_name="$(basename "$skill_dir")"
  mkdir -p "$REPO_ROOT/.claude/skills/$skill_name"
  cp "$skill_dir"* "$REPO_ROOT/.claude/skills/$skill_name/" 2>/dev/null || true
  echo "  Installed skill: $skill_name"
done

# ── 4. Copy hooks ──────────────────────────────────────────────────────
mkdir -p "$REPO_ROOT/.claude/hooks"

# Copy all hook scripts from the tusk source repo
for hookfile in "$SCRIPT_DIR"/.claude/hooks/*; do
  [[ -f "$hookfile" ]] || continue
  hookname="$(basename "$hookfile")"
  cp "$hookfile" "$REPO_ROOT/.claude/hooks/$hookname"
  chmod +x "$REPO_ROOT/.claude/hooks/$hookname"
  echo "  Installed .claude/hooks/$hookname"
done

# Override setup-path.sh for target projects — source version adds bin/ to PATH,
# but installed projects need .claude/bin/ on PATH instead.
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

# ── 4b. Merge hook registrations into .claude/settings.json ──────────
python3 -c "
import json, os

source_settings_path = os.path.join('$SCRIPT_DIR', '.claude', 'settings.json')
target_settings_path = os.path.join('$REPO_ROOT', '.claude', 'settings.json')

# Read source hook registrations
with open(source_settings_path) as f:
    source_hooks = json.load(f).get('hooks', {})

# Read existing target settings (or start fresh)
if os.path.exists(target_settings_path):
    with open(target_settings_path) as f:
        target_settings = json.load(f)
else:
    target_settings = {}

target_hooks = target_settings.setdefault('hooks', {})

# For each event type, merge hook groups from source into target
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
        if not any(cmd in existing_commands for cmd in group_commands if cmd):
            target_groups.append(group)
            for cmd in group_commands:
                if cmd:
                    print(f'  Registered hook: {cmd}')
        else:
            for cmd in group_commands:
                if cmd:
                    print(f'  Hook already registered: {cmd}')

with open(target_settings_path, 'w') as f:
    json.dump(target_settings, f, indent=2)
    f.write('\n')
"

# ── 5. Init database + migrate ───────────────────────────────────────
TUSK="$REPO_ROOT/.claude/bin/tusk"
"$TUSK" init
"$TUSK" migrate

# ── 6. Print next steps ───────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  Installation complete!"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "Next steps:"
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
echo ""
