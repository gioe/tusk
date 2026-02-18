#!/bin/bash
# PreToolUse hook: blocks != in SQL contexts.
# Shell history expansion breaks != — use <> instead.

# Read JSON from stdin
input=$(cat)

# Extract the command from tool_input.command
command=$(echo "$input" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('tool_input', {}).get('command', ''))
" 2>/dev/null)

# Quick exit: no != means nothing to check
echo "$command" | grep -q '!=' || exit 0

# Only block when tusk is being invoked (the only sanctioned way to run SQL).
# This avoids false positives on git commits, echo strings, etc. that happen
# to mention != alongside SQL keywords as documentation text.
if echo "$command" | grep -qE '(^|[|;&]|&&|\|\||\$\()\s*(bin/)?tusk\b'; then
  echo "Use <> instead of != in SQL — shell history expansion breaks !=."
  exit 2
fi

exit 0
