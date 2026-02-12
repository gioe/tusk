#!/usr/bin/env bash
#
# Install claude-taskdb into a Claude Code project.
#
# Usage:
#   cd /path/to/your/project
#   /path/to/claude-taskdb/install.sh
#
# What it does:
#   1. Copies bin/taskdb         → .claude/bin/taskdb
#   2. Copies skills/*           → .claude/skills/*
#   3. Copies scripts/*          → scripts/*  (creates if needed)
#   4. Copies config.default.json alongside bin for fallback
#   5. Runs taskdb init (creates DB + config if missing)
#   6. Prints CLAUDE.md snippet to paste into your project

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Must be run from a git repo root
if ! git rev-parse --show-toplevel &>/dev/null; then
  echo "Error: Run this from a git repository root." >&2
  exit 1
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
echo "Installing claude-taskdb into $REPO_ROOT"

# ── 1. Copy bin ──────────────────────────────────────────────────────
mkdir -p "$REPO_ROOT/.claude/bin"
cp "$SCRIPT_DIR/bin/taskdb" "$REPO_ROOT/.claude/bin/taskdb"
chmod +x "$REPO_ROOT/.claude/bin/taskdb"
echo "  Installed .claude/bin/taskdb"

# ── 2. Copy config default (fallback for bin) ────────────────────────
cp "$SCRIPT_DIR/config.default.json" "$REPO_ROOT/.claude/bin/config.default.json"
# Also update INSTALL_DIR logic — the default config lives next to the binary
echo "  Installed .claude/bin/config.default.json"

# ── 3. Copy skills ───────────────────────────────────────────────────
for skill_dir in "$SCRIPT_DIR"/skills/*/; do
  skill_name="$(basename "$skill_dir")"
  mkdir -p "$REPO_ROOT/.claude/skills/$skill_name"
  cp "$skill_dir"* "$REPO_ROOT/.claude/skills/$skill_name/" 2>/dev/null || true
  echo "  Installed skill: $skill_name"
done

# ── 4. Copy scripts ──────────────────────────────────────────────────
mkdir -p "$REPO_ROOT/scripts"
for script in "$SCRIPT_DIR"/scripts/*.py; do
  [[ -f "$script" ]] || continue
  script_name="$(basename "$script")"
  if [[ -f "$REPO_ROOT/scripts/$script_name" ]]; then
    echo "  Skipped scripts/$script_name (already exists)"
  else
    cp "$script" "$REPO_ROOT/scripts/$script_name"
    echo "  Installed scripts/$script_name"
  fi
done

# ── 5. Init database ─────────────────────────────────────────────────
"$REPO_ROOT/.claude/bin/taskdb" init

# ── 6. Print CLAUDE.md snippet ───────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  Installation complete!"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "Next steps:"
echo ""
echo "  1. Edit taskdb/config.json to set your project's domains and agents"
echo ""
echo "  2. Re-init to apply config changes:"
echo "     .claude/bin/taskdb init --force"
echo ""
echo "  3. Add this to your CLAUDE.md:"
echo ""
cat <<'SNIPPET'
## Task Queue

The project task database is managed via `.claude/bin/taskdb`. Use it for all task operations:

```bash
.claude/bin/taskdb "SELECT ..."          # Run SQL
.claude/bin/taskdb -header -column "SQL"  # With formatting flags
.claude/bin/taskdb path                   # Print resolved DB path
.claude/bin/taskdb config                 # Print project config
.claude/bin/taskdb config domains         # List valid domains
.claude/bin/taskdb init                   # Bootstrap DB (new projects)
.claude/bin/taskdb shell                  # Interactive sqlite3 shell
```

Never hardcode the DB path — always go through `taskdb`.
SNIPPET
echo ""
