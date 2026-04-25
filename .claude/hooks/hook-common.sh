#!/bin/bash
# Shared helper for .claude/hooks/ scripts. Sourced — never executed directly.
#
# Sets two variables on success:
#   REPO_ROOT — git toplevel of the current repo
#   TUSK      — path to the tusk binary
#
# Resolution order for TUSK:
#   $REPO_ROOT/bin/tusk          (source repo — preferred so hooks always run
#                                 the same tusk version as the tree they edit)
#   $REPO_ROOT/.claude/bin/tusk  (target project install layout)
#   command -v tusk              (PATH last resort; may be unset during
#                                 SessionStart before setup-path.sh runs)
#
# When the script runs outside a git repo, or no tusk binary resolves, the
# helper exits 0 so the calling hook becomes a silent no-op. Hooks that need
# additional logic before the tusk lookup (e.g. dupe-gate.sh's INSERT filter,
# inject-task-context.sh's TUSK_NO_SESSION_CONTEXT bypass) should perform
# those checks before sourcing this file.

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0

if [ -x "$REPO_ROOT/bin/tusk" ]; then
  TUSK="$REPO_ROOT/bin/tusk"
elif [ -x "$REPO_ROOT/.claude/bin/tusk" ]; then
  TUSK="$REPO_ROOT/.claude/bin/tusk"
elif command -v tusk &>/dev/null; then
  TUSK=tusk
else
  exit 0
fi
