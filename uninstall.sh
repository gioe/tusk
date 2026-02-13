#!/usr/bin/env bash
#
# Uninstall tusker from a Claude Code project.
#
# Usage:
#   cd /path/to/your/project
#   /path/to/tusker/uninstall.sh
# What it removes:
#   1. tusk + config.default.json
#   2. .claude/skills/{check-dupes,groom-backlog,manage-dependencies,next-task,tasks}
#   3. scripts/manage_dependencies.py
#   4. tusk/ directory (database + config) — requires --delete-data flag
#
# Cleans up empty parent directories (.claude/bin, .claude/skills, scripts)
# if nothing else remains in them.

set -euo pipefail

DELETE_DATA=false
for arg in "$@"; do
  case "$arg" in
    --delete-data) DELETE_DATA=true ;;
    -h|--help)
      echo "Usage: uninstall.sh [--delete-data]"
      echo ""
      echo "  --delete-data  Also remove tusk/ (database + config)"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

# Must be run from a git repo root
if ! git rev-parse --show-toplevel &>/dev/null; then
  echo "Error: Run this from a git repository root." >&2
  exit 1
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
echo "Uninstalling tusker from $REPO_ROOT"

removed=0

# Helper: remove a file and report
remove_file() {
  local f="$1"
  if [[ -f "$REPO_ROOT/$f" ]]; then
    rm "$REPO_ROOT/$f"
    echo "  Removed $f"
    removed=$((removed + 1))
  fi
}

# Helper: remove a directory and report
remove_dir() {
  local d="$1"
  if [[ -d "$REPO_ROOT/$d" ]]; then
    rm -rf "$REPO_ROOT/$d"
    echo "  Removed $d/"
    removed=$((removed + 1))
  fi
}

# Helper: remove directory if empty
rmdir_if_empty() {
  local d="$1"
  if [[ -d "$REPO_ROOT/$d" ]] && [[ -z "$(ls -A "$REPO_ROOT/$d")" ]]; then
    rmdir "$REPO_ROOT/$d"
    echo "  Cleaned up empty $d/"
  fi
}

# ── 1. Remove bin files ──────────────────────────────────────────────
remove_file "tusk"
remove_file ".claude/bin/config.default.json"
rmdir_if_empty ".claude/bin"

# ── 2. Remove skills ────────────────────────────────────────────────
for skill in check-dupes groom-backlog manage-dependencies next-task tasks; do
  remove_dir ".claude/skills/$skill"
done
rmdir_if_empty ".claude/skills"

# ── 3. Remove scripts ───────────────────────────────────────────────
remove_file "scripts/manage_dependencies.py"
rmdir_if_empty "scripts"

# ── 4. Remove data (opt-in) ─────────────────────────────────────────
if [[ "$DELETE_DATA" = true ]]; then
  remove_dir "tusk"
else
  if [[ -d "$REPO_ROOT/tusk" ]]; then
    echo ""
    echo "  Note: tusk/ directory preserved (contains your database)."
    echo "  Re-run with --delete-data to remove it."
  fi
fi

# ── Clean up .claude/ if empty ──────────────────────────────────────
rmdir_if_empty ".claude"

echo ""
if [[ $removed -eq 0 ]]; then
  echo "Nothing to uninstall — tusker does not appear to be installed."
else
  echo "Uninstall complete ($removed items removed)."
  echo ""
  echo "Don't forget to remove the Task Queue section from your CLAUDE.md if present."
fi
