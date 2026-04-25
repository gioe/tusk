#!/bin/bash
# PreToolUse hook: injects relevant conventions into Claude's context before Edit|Write.
# Non-blocking — always exits 0. Matching conventions surface as additionalContext.

source "$(dirname "$0")/hook-common.sh"

input=$(cat)

# Extract the file path from tool_input
file_path=$(echo "$input" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('tool_input', {}).get('file_path', ''))
" 2>/dev/null)

[ -z "$file_path" ] && exit 0

# Run conventions inject and capture output
conventions_output=$("$TUSK" conventions inject "$file_path" 2>/dev/null)

# No matching conventions — nothing to report
[ -z "$conventions_output" ] && exit 0

# Return matching conventions as additionalContext
python3 -c "
import json, sys
ctx = sys.argv[1]
path = sys.argv[2]
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'additionalContext': 'Relevant conventions for ' + path + ':\n' + ctx
    }
}))
" "$conventions_output" "$file_path"

exit 0
