#!/bin/bash
# PreToolUse hook: blocks direct sqlite3 invocations.
# All DB access should go through bin/tusk.

# Read JSON from stdin
input=$(cat)

# Extract the command and strip the contents of matched quote pairs
# (both double and single) so quoted string literals can't be mistaken for
# shell operators + sqlite3 (e.g. grep -E "foo|sqlite3" or commit messages
# that mention sqlite3). Strip double-quoted runs first (they can contain
# literal single quotes), then single-quoted runs.
command=$(echo "$input" | python3 -c "
import sys, json, re
data = json.load(sys.stdin)
cmd = data.get('tool_input', {}).get('command', '')
cmd = re.sub(r'\"[^\"]*\"', '\"\"', cmd)
cmd = re.sub(r\"'[^']*'\", \"''\", cmd)
print(cmd)
" 2>/dev/null)

# Check if sqlite3 is invoked in command position (after start-of-line,
# pipe, semicolon, &&, ||, or $() — not inside quoted strings.
if echo "$command" | grep -qE '(^|[|;&]|&&|\|\||\$\()\s*sqlite3\b'; then
  echo "Direct sqlite3 invocations are blocked." >&2
  echo "Use tusk CLI commands instead:" >&2
  echo "  tusk task-list" >&2
  echo "  tusk task-get <id>" >&2
  echo "  tusk task-done <id> --reason <reason>" >&2
  echo "  tusk task-update <id> ..." >&2
  exit 2
fi

exit 0
