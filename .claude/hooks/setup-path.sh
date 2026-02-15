#!/bin/bash
# Adds bin/ to PATH for Claude Code sessions in the tusk source repo
if [ -n "$CLAUDE_ENV_FILE" ]; then
  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  echo "export PATH=\"$REPO_ROOT/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi
exit 0
