#!/bin/bash
# PreToolUse hook: blocks git push on branches that don't follow the
# feature/TASK-<id>-<slug> naming convention. Exits 2 to block the push.
# main, master, and release/* are always allowed.

input=$(cat)

command=$(echo "$input" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('tool_input', {}).get('command', ''))
" 2>/dev/null)

# Quick exit: only trigger on git push commands in command position
echo "$command" | grep -qE '(^|[|;&]|&&|\|\||\$\()\s*git\s+push\b' || exit 0

# Get current branch
branch=$(git branch --show-current 2>/dev/null)

# Detached HEAD: branch name is empty — not worth policing, allow the push
if [[ -z "$branch" ]]; then
  exit 0
fi

# Allow main, master, and release/* branches without restriction
if [[ "$branch" == "main" || "$branch" == "master" || "$branch" == release/* ]]; then
  exit 0
fi

# All other branches must match feature/TASK-<id>-<slug>
if [[ "$branch" =~ ^feature/TASK-[0-9]+-. ]]; then
  exit 0
fi

echo "Branch '$branch' does not match required pattern 'feature/TASK-<id>-<slug>'. Create a branch with: git checkout -b feature/TASK-<id>-<slug>"
exit 2
