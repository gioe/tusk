#!/bin/bash
# PreToolUse hook: warns when distributable files (bin/, skills/,
# config.default.json, install.sh) changed since the remote default branch
# but VERSION was not bumped. Advisory only — exits 0 so the push is never blocked.

input=$(cat)

command=$(echo "$input" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('tool_input', {}).get('command', ''))
" 2>/dev/null)

# Quick exit: only trigger on git push commands in command position
echo "$command" | grep -qE '(^|[|;&]|&&|\|\||\$\()\s*git\s+push\b' || exit 0

# Detect the remote default branch dynamically; fall back to main
default_branch=$(git remote show origin 2>/dev/null | awk '/HEAD branch/ {print $NF}')
[[ -z "$default_branch" ]] && default_branch="main"

# Get list of files changed since the remote default branch
changed=$(git diff --name-only "origin/${default_branch}..HEAD" 2>/dev/null)

# Quick exit: no diff found (branch not found, empty repo, etc.)
[[ -z "$changed" ]] && exit 0

# Check if any distributable file changed
dist_changed=0
while IFS= read -r f; do
  case "$f" in
    bin/*|skills/*|config.default.json|install.sh)
      dist_changed=1
      break
      ;;
  esac
done <<< "$changed"

[[ "$dist_changed" -eq 0 ]] && exit 0

# Check if VERSION was also changed
if echo "$changed" | grep -qx 'VERSION'; then
  exit 0
fi

echo "Warning: distributable files (bin/, skills/, config.default.json, or install.sh) changed but VERSION was not bumped. Bump VERSION and update CHANGELOG before pushing."
exit 0
