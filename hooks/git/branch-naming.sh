#!/bin/bash
# Git pre-push guard: blocks pushes from branches that do not follow the
# feature/TASK-<id>-<slug> naming convention. main, master, and release/*
# are always allowed.
#
# Exit 2 on violation, 0 otherwise. Bypass with `git push --no-verify`.

branch=$(git branch --show-current 2>/dev/null)

# Detached HEAD: allow (nothing to police)
if [ -z "$branch" ]; then
  exit 0
fi

case "$branch" in
  main|master) exit 0 ;;
  release/*)   exit 0 ;;
esac

if [[ "$branch" =~ ^feature/TASK-[0-9]+-. ]]; then
  exit 0
fi

echo "ERROR: branch '$branch' does not match required pattern 'feature/TASK-<id>-<slug>'." >&2
echo "Create a branch with: tusk branch <task_id> <slug>" >&2
echo "If this is intentional, bypass with: git push --no-verify" >&2
exit 2
