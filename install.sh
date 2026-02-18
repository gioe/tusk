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
#   4. Installs SessionStart hooks (PATH setup + task context injection)
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

# ── 4. Install PATH hook ──────────────────────────────────────────────
mkdir -p "$REPO_ROOT/.claude/hooks"
cat > "$REPO_ROOT/.claude/hooks/tusk-path.sh" << 'HOOKEOF'
#!/bin/bash
# Added by tusk install — puts .claude/bin on PATH for Claude Code sessions
if [ -n "$CLAUDE_ENV_FILE" ]; then
  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  echo "export PATH=\"$REPO_ROOT/.claude/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi
exit 0
HOOKEOF
chmod +x "$REPO_ROOT/.claude/hooks/tusk-path.sh"
echo "  Installed .claude/hooks/tusk-path.sh"

# Merge SessionStart hook into .claude/settings.json
python3 -c "
import json, os

settings_path = os.path.join('$REPO_ROOT', '.claude', 'settings.json')
hook_entry = {'type': 'command', 'command': '.claude/hooks/tusk-path.sh'}

if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)
else:
    settings = {}

hooks = settings.setdefault('hooks', {})
session_start = hooks.setdefault('SessionStart', [])

already_installed = any(
    h.get('command') == '.claude/hooks/tusk-path.sh'
    for group in session_start
    for h in group.get('hooks', [])
)

if not already_installed:
    session_start.append({'hooks': [hook_entry]})
    with open(settings_path, 'w') as f:
        json.dump(settings, f, indent=2)
        f.write('\n')
    print('  Updated .claude/settings.json with PATH hook')
else:
    print('  .claude/settings.json already has PATH hook')
"

# ── 4b. Install task-context hook ────────────────────────────────────
cat > "$REPO_ROOT/.claude/hooks/inject-task-context.sh" << 'HOOKEOF'
#!/bin/bash
# Added by tusk install — shows in-progress tasks at session start
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
if [ -x "$REPO_ROOT/bin/tusk" ]; then
  TUSK="$REPO_ROOT/bin/tusk"
elif [ -x "$REPO_ROOT/.claude/bin/tusk" ]; then
  TUSK="$REPO_ROOT/.claude/bin/tusk"
else
  exit 0
fi

result=$("$TUSK" -json "
SELECT t.id, t.summary, t.complexity,
       p.commit_hash, p.next_steps, p.created_at AS progress_at
FROM tasks t
LEFT JOIN task_progress p ON p.task_id = t.id
  AND p.id = (SELECT MAX(p2.id) FROM task_progress p2 WHERE p2.task_id = t.id)
WHERE t.status = 'In Progress'
ORDER BY t.id;
" 2>/dev/null)

if [ -z "$result" ] || [ "$result" = "[]" ]; then
  exit 0
fi

ROWS="$result" python3 << 'PYEOF'
import os, json, sys

rows = json.loads(os.environ.get("ROWS", "[]"))
if not rows:
    sys.exit(0)

lines = ["=== Active Tasks ===", ""]
for r in rows:
    tid = r["id"]
    summary = r["summary"]
    complexity = r.get("complexity") or "?"
    commit = r.get("commit_hash") or None
    next_steps = r.get("next_steps") or None

    lines.append(f"TASK-{tid} [{complexity}]: {summary}")
    if commit:
        lines.append(f"  Last commit: {commit[:8]}")
    if next_steps:
        lines.append(f"  Next steps: {next_steps}")
    lines.append("")

print("\n".join(lines))
PYEOF
exit 0
HOOKEOF
chmod +x "$REPO_ROOT/.claude/hooks/inject-task-context.sh"
echo "  Installed .claude/hooks/inject-task-context.sh"

# Merge task-context hook into .claude/settings.json
python3 -c "
import json, os

settings_path = os.path.join('$REPO_ROOT', '.claude', 'settings.json')
hook_entry = {'type': 'command', 'command': '.claude/hooks/inject-task-context.sh'}

if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)
else:
    settings = {}

hooks = settings.setdefault('hooks', {})
session_start = hooks.setdefault('SessionStart', [])

already_installed = any(
    h.get('command') == '.claude/hooks/inject-task-context.sh'
    for group in session_start
    for h in group.get('hooks', [])
)

if not already_installed:
    session_start.append({'hooks': [hook_entry]})
    with open(settings_path, 'w') as f:
        json.dump(settings, f, indent=2)
        f.write('\n')
    print('  Updated .claude/settings.json with task-context hook')
else:
    print('  .claude/settings.json already has task-context hook')
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
